"""
DeepMancho — Worker de análisis de audio (server-side).

Procesa una cola `analysis_jobs` en Supabase:
  1. Reclama un job pendiente (RPC atómico claim_analysis_job).
  2. Descarga el audio del track desde Storage (rendition o master).
  3. Analiza con librosa: BPM, key (Camelot), beatgrid, waveform (peaks/rms/bandas),
     8 cue points "DeepMancho Standard", energy 1-10.
  4. Escribe los resultados en music_tracks y marca el job como 'done'.

100% offline respecto al navegador del DJ: escala a cientos de tracks sin cargar su equipo,
y con mejor calidad de cues/beat/key que las heurísticas de Web Audio.

Config por variables de entorno:
  SUPABASE_URL           (obligatorio)  ej: https://xxxx.supabase.co
  SUPABASE_SERVICE_KEY   (obligatorio)  service_role key (SECRETO)
  MUSIC_BUCKET           (opcional, default 'music')
  POLL_INTERVAL_SECONDS  (opcional, default 5)
  MAX_ATTEMPTS           (opcional, default 3)
"""

import os
import re
import sys
import time
import math
import random
import re
import json
import tempfile
import shutil
import traceback
import subprocess
import gc
import ctypes

import numpy as np
import librosa
from grid_detect import detect_grid
import requests

# ----------------------------------------------------------------------------
# Config — el worker habla con dos Edge Functions (worker-next / worker-result).
# NO necesita service key de Supabase: se autentica con un secreto compartido.
# ----------------------------------------------------------------------------
# WORKER_API_URL: base de las funciones, ej: https://TU-PROYECTO.supabase.co/functions/v1
WORKER_API_URL = os.environ.get("WORKER_API_URL", "").rstrip("/")
WORKER_SECRET = os.environ.get("WORKER_SECRET", "")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL_SECONDS", "5"))
POLL_MAX = float(os.environ.get("POLL_MAX_SECONDS", "120"))  # tope de la espera creciente
# Fase 2 (opcional): identificación por huella acústica (Chromaprint + AcoustID).
# Si no está la key o falta `fpcalc`, el worker sigue funcionando igual sin identificar.
ACOUSTID_API_KEY = os.environ.get("ACOUSTID_API_KEY", "")

SR = 11025          # más liviano que 22050; suficiente para beat/estructura/energía
MAX_DURATION = 600  # analiza como máximo 10 min (tope de tiempo/memoria)
# Resolución de la waveform. 800 se veía en bloques al hacer zoom en el mezclador;
# 3000 da ~4x de detalle para el zoom por compás sin inflar demasiado el payload.
BUCKETS = 3000
HEADERS = {"x-worker-secret": WORKER_SECRET, "Content-Type": "application/json"}


# ── Registros sin firmas (W6, 2-oct-2026) ────────────────────────────────────
# Los errores de requests/urllib traen la URL firmada completa ("…for url: https://…?token=eyJ…")
# y terminaban en los registros de Railway y en el `error` que se guarda en la base.
# Copia idéntica en worker.py, stems_worker.py y grid_verifier.py: cada imagen copia solo su archivo.
_FIRMAS = re.compile(
    r"(?i)((?:token|signature|x-amz-signature|x-amz-credential|x-amz-security-token|apikey)=)[^&\s'\"]+"
    r"|eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]+"
)


def sin_firma(texto) -> str:
    """El texto con los tokens, firmas y JWT tapados (`token=***`)."""
    return _FIRMAS.sub(lambda m: (m.group(1) + "***") if m.group(1) else "***", str(texto))


class _SalidaSinFirmas:
    def __init__(self, salida):
        self._salida = salida

    def write(self, texto):
        return self._salida.write(sin_firma(texto))

    def __getattr__(self, nombre):
        return getattr(self._salida, nombre)


def filtrar_salida():
    """Tapa firmas en todo lo que se imprime (print, log y traceback.print_exc)."""
    if not isinstance(sys.stdout, _SalidaSinFirmas):
        sys.stdout = _SalidaSinFirmas(sys.stdout)
    if not isinstance(sys.stderr, _SalidaSinFirmas):
        sys.stderr = _SalidaSinFirmas(sys.stderr)


class Espera:
    """Espera creciente con la cola vacía (W2, 30-sep-2026): arranca en `base`, se
    duplica en cada vuelta sin trabajo hasta `tope` y vuelve a `base` al recibirlo.
    Con espera fija, los workers hacían ~73.000 consultas al día sin nada que hacer.
    Copia idéntica en worker.py, stems_worker.py y grid_verifier.py: cada imagen
    copia solo su archivo."""

    def __init__(self, base: float, tope: float):
        self.base = max(1.0, float(base))
        self.tope = max(self.base, float(tope))
        self.actual = self.base

    def trabajo(self):
        self.actual = self.base

    def vacia(self) -> float:
        s = self.actual
        self.actual = min(self.tope, self.actual * 2)
        return s

    def error(self) -> float:
        # Mismo crecimiento, con ±20 % para que las réplicas no reintenten a la vez.
        return self.vacia() * random.uniform(0.8, 1.2)

if not WORKER_API_URL or not WORKER_SECRET:
    print("ERROR: faltan WORKER_API_URL o WORKER_SECRET", flush=True)
    sys.exit(1)

# ----------------------------------------------------------------------------
# CM2 (v6) — Ancla de rejilla de precisión sobre la RENDITION + examen golden set
# CM1-bis (v6) — Restauración del cálculo de loudness (LUFS), perdido en la
#                reescritura v5 (regresión detectada el 18-ago: jobs 'done'
#                sin llenar loudness_lufs).
#
# Diagnóstico que motiva CM2 (18-ago-2026, sesión de mixer con telemetría):
#   - El motor del mixer alinea bien (test mismo-track = perfecto).
#   - El ancla de rejilla por track tiene errores de 40-120 ms porque:
#     (a) el análisis corre a SR=11025 (~46 ms por frame), y
#     (b) la rejilla se calcula sobre el máster, pero el navegador
#         reproduce la rendition (timeline distinto por encoder delay).
#   - Se validó de punta a punta que corregir SOLO el dato arregla la mezcla
#     (par Right Thing × Till There Was You: phaseMs 118.5 -> ~0).
#
# Por eso CM2: (1) calcula el ancla a 22050 Hz / hop 128 (~5.8 ms de frame,
# con ajuste de fase sobre todo el track -> precisión de pocos ms), (2) la
# calcula sobre el MISMO audio que sirve stream-track (lo que oye el DJ), y
# (3) antes de tocar el catálogo, rinde un EXAMEN contra 6 tracks calibrados
# por el oído del DJ ("golden set"). Sin examen aprobado no hay backfill.
# ----------------------------------------------------------------------------
ENABLE_SET_RENDER = os.environ.get("ENABLE_SET_RENDER", "").lower() == "true"
SET_SR = 44100           # SR de render del set (calidad final, no analisis)
XFADE_BARS = 16          # duracion objetivo de transicion, en compases
MIN_XFADE_BARS = 8
MAX_STRETCH_PCT = 6.0    # tope de time-stretch: mas alla los artefactos se oyen
BASS_HZ = 70.0           # low-shelf del bass-swap (referencia DJM-800)
SET_TARGET_LUFS = -14.0

GOLDEN_EXAM = os.environ.get("GOLDEN_EXAM", "true").lower() != "false"
ENABLE_ANCHOR_BACKFILL = os.environ.get("ENABLE_ANCHOR_BACKFILL", "").lower() == "true"
EXAMEN_CM2_APROBADO = False  # lo fija main() con el resultado de golden_exam()


def cm2_habilitado() -> bool:
    """CM2 escribe anclas solo con la variable Y el examen golden aprobado en este
    arranque (7.5.3). Antes bastaba la variable: el examen no bloqueaba nada."""
    return ENABLE_ANCHOR_BACKFILL and EXAMEN_CM2_APROBADO
ENABLE_MIX_V7 = os.environ.get("ENABLE_MIX_V7", "0") == "1"  # v7.1 MIX-IN/OUT refutados: apagados por default
ANCHOR_SR = 22050    # SR del análisis de ancla (independiente del SR=11025 general)
ANCHOR_HOP = 128     # ~5.8 ms por frame de onset a 22050 Hz
ANCHOR_TOL_MS = 10.0 # criterio del examen (error relativo por par)

# Golden set SINTÉTICO (#573, 3-oct-2026). Antes eran 6 temas del catálogo (calibrados
# por oído el 23-ago, ver docs/golden-set-mixer.md); al pasar a la papelera y quedar su
# audio solo en el proyecto viejo, stream-track daba 404 y el examen salía NO APROBADO en
# cada arranque. Ahora cada tema se GENERA con BPM y fase de bombo conocidos y se codifica
# con make_rendition (el mismo MP3 que oye el DJ), así que el examen sigue midiendo el
# ancla sobre la rendición sin depender de la música de nadie ni de la red.
# Cada tema trae trampas de música real: intro y break sin bombo, bajo a contratiempo
# DENTRO de la banda del bombo (35-130 Hz), clap en 2 y 4, hi-hats y ruido de fondo.
# El oro es el arranque del bombo (su primera muestra), módulo el período de beat.
GOLDEN_SINTETICO = [
    # (nombre, bpm, fase_ms, kick_hz_ini, caida_kick, nivel_bajo)
    ("sintético 122 · bombo seco",        122.0,  12.0, 160.0, 14.0, 0.30),
    ("sintético 122 · bombo largo",       122.0,  57.0, 120.0,  8.0, 0.35),
    ("sintético 124 · bajo fuerte",       124.0,  98.0, 150.0, 12.0, 0.55),
    ("sintético 124 · bombo grave",       124.0,  41.0,  90.0, 10.0, 0.30),
    ("sintético 128 · fase tardía",       128.0, 371.0, 170.0, 16.0, 0.40),
    ("sintético 123 · casi sin ataque",   123.0, 203.0, 100.0,  6.0, 0.45),
]

# El ancla del examen se compara MODULO el periodo de beat: el valor absoluto que
# reporta el worker (p. ej. 16284.3 ms) es el mismo ancla + n*beat.
GOLDEN_PAIRS = [(0, 1), (2, 3), (4, 5)]  # indices (deck A, deck B); el orden fija el signo.


def tema_golden(bpm, fase_ms, kick_hz, caida, nivel_bajo, dur_s=120.0, sr=44100, semilla=0):
    """Tema 4x4 sintético en estéreo float32 con el bombo arrancando en fase_ms + n*beat.
    8 compases de intro y 8 de break (compases 40-47) sin bombo."""
    rng = np.random.default_rng(semilla)
    n = int(dur_s * sr)
    t = np.arange(n) / sr
    beat = 60.0 / bpm
    x = np.zeros(n, dtype=np.float64)
    largo = int(0.35 * sr)
    tk = np.arange(largo) / sr
    ataque = np.minimum(1.0, tk / 0.002)                       # 2 ms de subida
    f = 45.0 + (kick_hz - 45.0) * np.exp(-tk * 30.0)           # barrido de tono del bombo
    bombo = np.sin(2 * np.pi * np.cumsum(f) / sr) * np.exp(-tk * caida) * ataque
    largo_c = int(0.12 * sr)
    clap = rng.standard_normal(largo_c) * np.exp(-np.arange(largo_c) / sr * 35.0)
    clap = clap - np.convolve(clap, np.ones(9) / 9, mode="same")  # sin graves
    largo_h = int(0.05 * sr)
    hat = rng.standard_normal(largo_h) * np.exp(-np.arange(largo_h) / sr * 90.0)
    hat = hat - np.convolve(hat, np.ones(5) / 5, mode="same")
    k = 0
    while True:
        t0 = fase_ms / 1000.0 + k * beat
        i0 = int(round(t0 * sr))
        if i0 >= n:
            break
        compas = k // 4
        sin_bombo = compas < 8 or 40 <= compas < 48
        if not sin_bombo and i0 >= 0:
            m = min(largo, n - i0); x[i0:i0 + m] += 0.9 * bombo[:m]
        if k % 4 in (1, 3) and compas >= 4 and i0 >= 0:            # clap en 2 y 4
            m = min(largo_c, n - i0); x[i0:i0 + m] += 0.25 * clap[:m]
        ih = int(round((t0 + beat / 2) * sr))                      # hi-hat y bajo a contratiempo
        if 0 <= ih < n:
            m = min(largo_h, n - ih); x[ih:ih + m] += 0.12 * hat[:m]
            if compas >= 8:
                lb = min(int(beat / 2 * sr * 0.9), n - ih)
                tb = np.arange(lb) / sr
                x[ih:ih + lb] += nivel_bajo * np.sin(2 * np.pi * 55.0 * tb) * np.minimum(1.0, tb / 0.01) \
                    * np.minimum(1.0, (lb / sr - tb) / 0.01)
        k += 1
    x += 0.003 * rng.standard_normal(n)                          # piso de ruido (~ -50 dB)
    x = 0.9 * x / max(1e-9, float(np.max(np.abs(x))))
    return np.stack([x, x * 0.97], axis=1).astype(np.float32)


# ----------------------------------------------------------------------------
# Estándar de 8 cues (debe coincidir con src/lib/djCueStandard.ts)
# ----------------------------------------------------------------------------
DJ_CUE_STANDARD = [
    (0, "MIX-IN", "#28E214"),
    (1, "BASS-IN", "#E6C800"),
    (2, "BUILD", "#FFA000"),
    (3, "DROP 1", "#E61414"),
    (4, "BREAK", "#AA50FF"),
    (5, "DROP 2", "#FF3264"),
    (6, "VOCALS", "#FFFFFF"),
    (7, "MIX-OUT", "#2864E2"),
]
CUE_DEF = {n: (label, color) for n, label, color in DJ_CUE_STANDARD}

# Perfiles Krumhansl-Schmuckler (calibrados con música CLÁSICA — se dejan como
# referencia/fallback, ya no se usan por defecto).
KS_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
KS_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

# ── CAMBIO 4 (v5.1) — perfiles de tonalidad para MÚSICA ELECTRÓNICA ──────────
# Perfiles 'edma' (Faraldo et al., proyecto GiantSteps): extraídos por análisis
# de corpus de EDM. En el benchmark GiantSteps (604 tracks de Beatport) superan
# a Krumhansl y a KeyFinder.
#
# Fuente de los coeficientes: código fuente de Essentia,
#   src/algorithms/tonal/key.cpp, arreglo `profileTypesWithOther`, entrada 'edma'.
# Copiados textualmente del archivo, NO de memoria.
#
# Se usan SOLO los doce números de cada perfil: no se importa Essentia (su
# licencia AGPLv3 exigiría licencia comercial de la UPF). Los coeficientes son
# datos publicados; la implementación de abajo es propia sobre librosa.
EDMA_MAJOR = np.array([1.00, 0.29, 0.50, 0.40, 0.60, 0.56, 0.32, 0.80, 0.31, 0.45, 0.42, 0.39])
EDMA_MINOR = np.array([1.00, 0.31, 0.44, 0.58, 0.33, 0.49, 0.29, 0.78, 0.43, 0.29, 0.53, 0.32])
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Nota (0=C) -> Camelot. Menores = letra A, mayores = letra B (rueda estándar).
MINOR_CAMELOT = {  # índice de nota (0=C) -> camelot menor
    0: "5A", 1: "12A", 2: "7A", 3: "2A", 4: "9A", 5: "4A",
    6: "11A", 7: "6A", 8: "1A", 9: "8A", 10: "3A", 11: "10A",
}
MAJOR_CAMELOT = {  # índice de nota (0=C) -> camelot mayor
    0: "8B", 1: "3B", 2: "10B", 3: "5B", 4: "12B", 5: "7B",
    6: "2B", 7: "9B", 8: "4B", 9: "11B", 10: "6B", 11: "1B",
}


# ----------------------------------------------------------------------------
# Utilidades DSP
# ----------------------------------------------------------------------------
def _norm_max(a: np.ndarray) -> np.ndarray:
    m = float(np.max(a)) if a.size else 0.0
    return (a / m) if m > 0 else a


def bucket_reduce(values: np.ndarray, buckets: int, mode: str) -> list:
    if values.size == 0:
        return []
    idx = np.linspace(0, values.size, buckets + 1).astype(int)
    out = np.zeros(buckets, dtype=np.float64)
    for b in range(buckets):
        s, e = idx[b], max(idx[b] + 1, idx[b + 1])
        seg = values[s:e]
        if seg.size == 0:
            out[b] = 0.0
        elif mode == "peak":
            out[b] = float(np.max(np.abs(seg)))
        else:  # rms
            out[b] = float(np.sqrt(np.mean(seg ** 2)))
    out = _norm_max(out)
    return [round(float(x), 4) for x in out]


def compute_bands(y: np.ndarray, sr: int, buckets: int) -> dict:
    """Picos por banda (bass<200, mid 200-2k, high>2k) en `buckets` cubos."""
    n_fft = 2048
    hop = max(1, len(y) // (buckets * 2))
    S = np.abs(librosa.stft(y, n_fft=n_fft, hop_length=hop))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    bass_mask = freqs < 200
    mid_mask = (freqs >= 200) & (freqs < 2000)
    high_mask = freqs >= 2000

    def band_series(mask):
        e = S[mask, :].sum(axis=0) if mask.any() else np.zeros(S.shape[1])
        # remuestrear a `buckets`
        if e.size == 0:
            return [0.0] * buckets
        idx = np.linspace(0, e.size, buckets + 1).astype(int)
        out = np.array([float(np.max(e[idx[b]:max(idx[b] + 1, idx[b + 1])]) or 0.0) for b in range(buckets)])
        out = _norm_max(out)
        return [round(float(x), 4) for x in out]

    return {"bass": band_series(bass_mask), "mid": band_series(mid_mask), "high": band_series(high_mask)}


def detect_key(y: np.ndarray, sr: int):
    """Correlación de perfiles sobre chroma. Devuelve (key_musical, camelot).

    CAMBIO 4 (v5.1): usa los perfiles 'edma' (calibrados con EDM) en vez de
    Krumhansl-Schmuckler (calibrado con música clásica). Mismo algoritmo, misma
    velocidad, mismo costo: cambian doce números por perfil.
    """
    try:
        chroma = librosa.feature.chroma_stft(y=y, sr=sr)  # más rápido que chroma_cqt
        prof = chroma.mean(axis=1)
        prof = prof / (prof.sum() + 1e-9)
        best = (-1e9, 0, True)
        for i in range(12):
            maj = np.corrcoef(np.roll(EDMA_MAJOR, i), prof)[0, 1]
            mino = np.corrcoef(np.roll(EDMA_MINOR, i), prof)[0, 1]
            if maj > best[0]:
                best = (maj, i, False)
            if mino > best[0]:
                best = (mino, i, True)
        _, note_idx, is_minor = best
        if is_minor:
            key = NOTE_NAMES[note_idx] + "m"
            cam = MINOR_CAMELOT[note_idx]
        else:
            key = NOTE_NAMES[note_idx]
            cam = MAJOR_CAMELOT[note_idx]
        return key, cam
    except Exception:
        return None, None


def compute_energy(rms_full: np.ndarray, bands: dict) -> int:
    """Energy 1-10 tipo Mixed In Key."""
    try:
        loud = float(np.mean(rms_full)) if rms_full.size else 0.0
        perceptual = math.sqrt(max(0.0, loud))
        high = np.array(bands.get("high", []), dtype=float)
        high_act = float(np.mean(high)) if high.size else 0.0
        e01 = max(0.0, min(1.0, 0.6 * min(1.0, perceptual * 3.0) + 0.4 * high_act))
        return int(max(1, min(10, round(1 + 9 * e01))))
    except Exception:
        return None


def _bar_band_energies(y, sr, anchor_ms, bar_ms, n_bars):
    """dB RMS por compás en 4 bandas. Filtro Butterworth de fase cero (sin
    corrimiento temporal: importa porque estos valores deciden POSICIONES)."""
    from scipy.signal import butter, sosfiltfilt
    BANDAS = {"low": (30, 130), "lowmid": (130, 300), "mid": (300, 3000), "high": (5000, 9000)}
    out = {}
    for nombre, (lo, hi) in BANDAS.items():
        hi = min(hi, sr / 2 - 100)
        if hi <= lo:
            out[nombre] = np.full(n_bars, -120.0)
            continue
        sos = butter(4, [lo / (sr / 2), hi / (sr / 2)], btype="band", output="sos")
        b = sosfiltfilt(sos, y.astype(np.float64))
        e = np.empty(n_bars)
        for i in range(n_bars):
            a0 = int((anchor_ms + i * bar_ms) * sr / 1000.0)
            a1 = int((anchor_ms + (i + 1) * bar_ms) * sr / 1000.0)
            seg = b[max(0, a0):max(0, a1)]
            e[i] = np.sqrt(np.mean(seg ** 2)) if seg.size else 1e-6
        out[nombre] = 20 * np.log10(np.maximum(e, 1e-6))
    return out


def _energia_1_10(db_val, p10, p90):
    """Escala la energia medida al 1-10 estilo Mixed In Key.
    Calibrado contra 7493 cues reales: MIK concentra sus valores en 4-6
    (Energy 6 n=3178, 5 n=2435, 4 n=1013), con extremos raros. Por eso el
    mapeo comprime hacia el centro en vez de repartir linealmente 1..10."""
    if p90 <= p10:
        return 5
    x = (db_val - p10) / (p90 - p10)          # 0..1 dentro del propio track
    x = max(0.0, min(1.0, x))
    return int(max(1, min(10, round(3.5 + 4.0 * x))))


def detect_cues(y: np.ndarray, sr: int, bpm, first_beat_ms):
    """v7.4 — 8 hot cues siguiendo la METODOLOGIA INFERIDA DE MIXED IN KEY.

    No copia posiciones: replica el metodo. Inferido de 951 canciones del
    catalogo con sus 7493 cues reales de MIK (export de Rekordbox):

      1. El cue A esta SIEMPRE en el segundo 0 (mediana 0.07 s; 94% < 1 s).
         Es el punto de carga, no un punto de mezcla.
      2. Los 8 cues caen SIEMPRE en la grilla de frases de 8 compases medida
         desde A. Afinando el BPM, el error de ajuste da 0.00 compases de
         mediana y el 65% de los tracks encaja perfecto.
      3. El espaciado NO es regular: 0% de los tracks tiene todos los saltos
         iguales, el 100% varia. El algoritmo ELIGE segun la musica.
         Saltos usados: 16 (n=1599), 8 (1202), 32 (851), 24 (596).
      4. Cobertura: el ultimo cue cae al ~79% de la duracion.
      5. Cada cue lleva un nivel de ENERGIA 1-10 (MIK concentra en 4-6).

    Perfil mediano de MIK que este detector reproduce (compas desde A):
      A=0 · B=32 · C=48 · D=64 · E=88 · F=104 · G=128 · H=151
    """
    if not bpm or bpm < 40:
        return None
    try:
        bar_ms = (60000.0 / bpm) * 4
        dur_ms = (len(y) / sr) * 1000.0
        # La GRILLA se cuenta desde el PRIMER BEAT REAL (ancla certificada), no
        # desde t=0. Medido sobre 10 tracks: contando desde cero, los cues caian
        # en compases 6.01, 18.01, 15.36... es decir, en multiplos de compas pero
        # DESFASADOS de la frase musical, porque el archivo arranca antes del
        # primer golpe. El cue A sigue yendo al segundo 0 (metodologia MIK), pero
        # los otros 7 se cuentan desde el ancla para caer en frases reales.
        anchor_ms = float(first_beat_ms or 0.0)
        if anchor_ms < 0 or anchor_ms > dur_ms:
            anchor_ms = 0.0
        n_bars = int((dur_ms - anchor_ms) // bar_ms)
        if n_bars < 24:
            return None

        F = _bar_band_energies(y, sr, anchor_ms, bar_ms, n_bars)
        tot = 20 * np.log10(np.maximum(
            np.sqrt(sum(10 ** (F[k] / 10) for k in F)), 1e-6))
        p10, p90 = float(np.percentile(tot, 10)), float(np.percentile(tot, 90))

        M = np.vstack([F[k] for k in ("low", "lowmid", "mid", "high")]).T
        M = (M - M.mean(0)) / (M.std(0) + 1e-6)

        PASO = 8                      # grilla de frase (regla 2)
        SALTOS = (8, 16, 24, 32)      # repertorio observado (regla 3)
        activos = np.where(tot >= p90 - 25)[0]
        fin_util = int(activos[-1]) if activos.size else n_bars - 1

        # Novedad estructural en cada limite de frase: 8 compases antes vs
        # despues. Es lo que hace que cada cancion tenga su propia huella.
        nov = {}
        for b in range(PASO, fin_util - 8, PASO):
            pre, post = M[max(0, b - 8):b], M[b:b + 8]
            if pre.size and post.size:
                nov[b] = float(np.linalg.norm(post.mean(0) - pre.mean(0)))
        if len(nov) < 4:
            return None

        # Objetivo de reparto: el perfil mediano medido en MIK, escalado a
        # este track. H apunta al ~79% del audio util (regla 4).
        # Perfil objetivo escalado al audio util. El ultimo valor era 0.79 (la
        # mediana medida en MIK) y resulto DEMASIADO CORTO: la validacion sobre
        # 180 tracks mostro que ese tope dejaba al detector sin candidatos antes
        # de los 8 cues (solo 113/180 llegaban a 8) y se comia el 9.4% de los
        # cues de MIK, que en 85 de 180 tracks pone cues despues del 79%.
        objetivo_rel = (0.0, 0.14, 0.25, 0.35, 0.46, 0.57, 0.68, 0.88)
        elegidos = [0]

        # --- H (SALIDA) se elige PRIMERO y con criterio propio ---------------
        # Prioridad del dueño: el cue de inicio y el de salida son los que mas
        # importan. H no puede ser "el ultimo que sobro": se busca el limite de
        # frase con mayor cambio musical en la ventana final (75-92% del audio
        # util), prefiriendo una CAIDA de energia sostenida (inicio del outro).
        v0, v1 = int(0.75 * fin_util), int(0.92 * fin_util)
        vent = [b for b in nov if v0 <= b <= v1]
        if vent:
            def score_h(b):
                antes = float(np.median(tot[max(0, b - 8):b]))
                despues = float(np.median(tot[b:b + 8]))
                caida = max(0.0, antes - despues) / 6.0      # bonus si baja
                return nov[b] / (max(nov.values()) or 1.0) + caida
            h_bar = max(vent, key=score_h)
        else:
            h_bar = ((fin_util - 8) // PASO) * PASO

        # --- B..G: recorren el perfil objetivo hasta llegar a H --------------
        for rel in objetivo_rel[1:-1]:
            ideal = rel * h_bar
            cands = []
            for salto in SALTOS:
                b = elegidos[-1] + salto
                if b in nov and b < h_bar and abs(b - ideal) <= 40:
                    cands.append(b)
            if not cands:
                cands = [b for b in nov
                         if b > elegidos[-1] and b < h_bar and abs(b - ideal) <= 24]
            if not cands:
                b = elegidos[-1] + 16
                if b >= h_bar:
                    break
                cands = [b]
            elegidos.append(max(cands, key=lambda b: nov.get(b, 0.0)))

        while len(elegidos) < 8:
            b = max(x for x in elegidos if x < h_bar) + 16 if any(x < h_bar for x in elegidos) else 16
            if b >= h_bar:
                b = max(x for x in elegidos if x < h_bar) + 8
            if b < h_bar and b not in elegidos:
                elegidos.append(b)
                continue
            # Sin lugar al final: partir el hueco mas grande por la mitad,
            # cuantizado a 8 compases, eligiendo el candidato mas "musical".
            elegidos = sorted(set(elegidos))
            huecos = [(elegidos[i + 1] - elegidos[i], i)
                      for i in range(len(elegidos) - 1)]
            if not huecos:
                break
            ancho, i = max(huecos)
            if ancho < 2 * PASO:      # sin lugar ni para un cue intermedio
                break
            lo, hi = elegidos[i], elegidos[i + 1]
            cands = [b for b in range(lo + 8, hi, PASO) if b not in elegidos]
            if not cands:
                break
            elegidos.append(max(cands, key=lambda b: nov.get(b, 0.0)))
        elegidos = sorted(set(b for b in elegidos if b < h_bar))[:7] + [h_bar]
        elegidos = sorted(set(elegidos))[:8]
        if len(elegidos) < 6:
            return None

        cues = []
        for num, b in enumerate(elegidos):
            # cue A = segundo 0 del archivo (metodologia MIK); el resto sobre la
            # grilla de frase medida desde el primer beat real.
            pos = 0 if num == 0 else int(round(anchor_ms + b * bar_ms))
            if pos >= dur_ms - 500:
                continue
            label, color = CUE_DEF[num]
            seg = tot[b:b + 8]
            energia = _energia_1_10(
                float(np.median(seg)) if seg.size else p10, p10, p90)
            n = nov.get(b, 0.0)
            nmax = max(nov.values()) or 1.0
            conf = 0.4 + 0.6 * min(1.0, n / nmax) if num else 1.0
            cues.append({
                "number": num, "label": label, "color": color,
                "positionMs": pos,
                "energy": energia,
                "confidence": round(float(min(1.0, conf)), 2),
            })
        return cues if len(cues) >= 6 else None
    except Exception as e:
        print(f"    detect_cues v7.4 fallo: {e}", flush=True)
        return None


# ----------------------------------------------------------------------------
# PLAN B y C de hot cues (v7.5). El detector estructural (detect_cues) exige
# >= 24 compases y contraste de energia; si no lo encuentra devolvia None y el
# tema quedaba SIN cues (bocetos, frases, correcciones, temas muy planos).
# Ahora nunca queda vacio:
#   B) rejilla de frases desde el ancla real: A en 0, H en la ultima frase que
#      deja cola, intermedios en los bordes de frase con mas cambio medido.
#   C) sin BPM: por tiempo (A en 0, H al 85 %).
# Ambos van con confidence < 0.5 y "origen": el mezclador no los usa para
# mezcla automatica (cueConfidence.ts) pero el DJ puede saltar a ellos, y el
# worker-result los etiqueta cue_source='grilla_v1' / 'tiempo_v1'.
# ----------------------------------------------------------------------------
CONF_RESPALDO = 0.3


def _cue_respaldo(num, pos_ms, energia, origen, conf=CONF_RESPALDO):
    label, color = CUE_DEF[num]
    return {"number": num, "label": label, "color": color, "positionMs": int(round(pos_ms)),
            "energy": int(energia), "confidence": 1.0 if num == 0 else conf, "origen": origen}


def cues_respaldo(y: np.ndarray, sr: int, bpm, first_beat_ms):
    """Plan B: cues sobre la rejilla de frases. None solo si no hay BPM."""
    if not bpm or not (40 < float(bpm) < 240):
        return None
    try:
        bar_ms = (60000.0 / float(bpm)) * 4
        dur_ms = (len(y) / sr) * 1000.0
        anchor = float(first_beat_ms or 0.0)
        if anchor < 0 or anchor > dur_ms:
            anchor = 0.0
        n_bars = int((dur_ms - anchor) // bar_ms)
        if n_bars < 2:
            return [_cue_respaldo(0, 0, 5, "grilla")]
        # Frase de 8 compases; en audio corto, de 4 (o 2) para que entren cues.
        paso = 8 if n_bars >= 32 else (4 if n_bars >= 8 else 2)
        F = _bar_band_energies(y, sr, anchor, bar_ms, n_bars)
        tot = 20 * np.log10(np.maximum(np.sqrt(sum(10 ** (F[k] / 10) for k in F)), 1e-6))
        p10, p90 = float(np.percentile(tot, 10)), float(np.percentile(tot, 90))
        M = np.vstack([F[k] for k in ("low", "lowmid", "mid", "high")]).T
        M = (M - M.mean(0)) / (M.std(0) + 1e-6)
        media = max(1, paso // 2)

        def novedad(b):
            pre, post = M[max(0, b - media):b], M[b:b + media]
            return float(np.linalg.norm(post.mean(0) - pre.mean(0))) if pre.size and post.size else 0.0

        def energia(b):
            seg = tot[b:b + paso]
            return _energia_1_10(float(np.median(seg)) if seg.size else p10, p10, p90)

        bordes = list(range(paso, n_bars, paso))
        # H: ultima frase que deja cola (16 compases o un cuarto del tema);
        # entre las candidatas del ultimo tercio, la de mayor caida de energia.
        cola = min(16, max(paso, n_bars // 4))
        cand_h = [b for b in bordes if n_bars - b >= cola and b >= n_bars * 0.6]
        if cand_h:
            def caida(b):
                return float(np.median(tot[max(0, b - paso):b])) - float(np.median(tot[b:b + paso]))
            h = max(cand_h, key=lambda b: (caida(b), b))
        else:
            h = bordes[-1] if bordes else None
        medios = [b for b in bordes if h is None or b < h]
        medios = sorted(sorted(medios, key=novedad, reverse=True)[:6])
        cues = [_cue_respaldo(0, 0, energia(0), "grilla")]
        for i, b in enumerate(medios, start=1):
            cues.append(_cue_respaldo(i, anchor + b * bar_ms, energia(b), "grilla"))
        if h is not None:
            cues.append(_cue_respaldo(7, anchor + h * bar_ms, energia(h), "grilla"))
        return [c for c in cues if c["positionMs"] < dur_ms - 250]
    except Exception as e:
        print(f"    cues_respaldo fallo: {e}", flush=True)
        return [_cue_respaldo(0, 0, 5, "grilla")]


def cues_por_tiempo(dur_ms: float):
    """Plan C: sin BPM no hay rejilla. A en 0 y H al 85 % del audio."""
    if not dur_ms or dur_ms < 4000:
        return [_cue_respaldo(0, 0, 5, "tiempo", 0.2)]
    return [_cue_respaldo(0, 0, 5, "tiempo", 0.2), _cue_respaldo(7, dur_ms * 0.85, 5, "tiempo", 0.2)]


def detect_vocal_segments(y: np.ndarray, sr: int, bpm, first_beat_ms):
    """Regiones (tramos) con voz — capa aparte de los cue points.
    Banda vocal ~300-3000 Hz sostenida y por encima de los agudos. Best-effort."""
    if not bpm or bpm < 40 or first_beat_ms is None:
        return None
    try:
        beat_ms = 60000.0 / bpm
        bar_ms = beat_ms * 4
        dur_ms = (len(y) / sr) * 1000.0
        hop = 512
        S = np.abs(librosa.stft(y, n_fft=2048, hop_length=hop))
        freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
        times = librosa.frames_to_time(np.arange(S.shape[1]), sr=sr, hop_length=hop) * 1000.0
        vocal = S[(freqs >= 300) & (freqs < 3000), :].sum(axis=0)
        high = S[freqs >= 3000, :].sum(axis=0)
        n_bars = int(max(0, (dur_ms - first_beat_ms) // bar_ms))
        if n_bars < 4:
            return None

        def bar_mean(arr, b):
            t0 = first_beat_ms + b * bar_ms
            t1 = t0 + bar_ms
            m = (times >= t0) & (times < t1)
            return float(np.mean(arr[m])) if m.any() else 0.0

        voc_b = _norm_max(np.array([bar_mean(vocal, b) for b in range(n_bars)]))
        hi_b = _norm_max(np.array([bar_mean(high, b) for b in range(n_bars)]))
        active = [bool(voc_b[b] > 0.45 and voc_b[b] > hi_b[b] * 1.15) for b in range(n_bars)]

        segments = []
        b = 0
        while b < n_bars:
            if active[b]:
                start = b
                while b < n_bars and active[b]:
                    b += 1
                if b - start >= 4:  # mínimo 4 compases para evitar falsos positivos
                    segments.append({
                        "startMs": int(round(first_beat_ms + start * bar_ms)),
                        "endMs": int(round(min(dur_ms, first_beat_ms + b * bar_ms))),
                    })
            else:
                b += 1
        return segments or None
    except Exception:
        return None


# ============================================================================
# v7 — PIPELINE "LISTO PARA MEZCLAR"
# ============================================================================
# Contexto (medido, no supuesto): 996/1005 tracks tenían bpm_fine=0, es decir
# BPM entero exacto. Un error de 0.24 BPM (el máximo observado) acumula UN BEAT
# de desfase en ~4 minutos:  t_a_un_beat = 60 / ΔBPM.  Por eso hay pares que
# arrancan alineados y "se van" a mitad de tema, aun con el ancla perfecta.
#
# Nota de licencia: NO se usa madmom. Sus modelos preentrenados son CC BY-NC-SA
# (no comercial). Todo esto es librosa (ISC) + numpy, apto para uso comercial.
# ----------------------------------------------------------------------------

SR_GRID = 22050          # SR del refinamiento de tempo (más resolución que SR=11025)
HOP_GRID = 128           # ~5.8 ms por frame
MIXOUT_MIN_PCT = 0.70    # el MIX-OUT nunca antes del 70% del track
RUNWAY_BARS_MIN = 16     # audio mínimo tras MIX-OUT para completar la mezcla
TEMPO_RESID_MS = 35.0    # residuo robusto (p90) para considerar el tempo constante


def refine_bpm(y22: np.ndarray, sr22: int, bpm_nominal: float):
    """Refina un BPM nominal (entero) a su valor real con decimales.

    Método: ajuste por mínimos cuadrados sobre los tiempos de beat detectados a
    lo largo de TODO el track. Si los beats son t_i ≈ t0 + i*periodo, la
    pendiente de la recta da el periodo real; el error del BPM escala con
    1/duración, así que sobre 5-7 min la resolución baja de 0.01 BPM.

    Devuelve (bpm_refinado, residuo_max_ms, n_beats) o (None, None, 0).
    """
    try:
        onset_env = librosa.onset.onset_strength(y=y22, sr=sr22, hop_length=HOP_GRID)
        # Anclar la búsqueda al nominal protegido: evita saltos de octava y de tresillo
        _, beats = librosa.beat.beat_track(onset_envelope=onset_env, sr=sr22,
                                           hop_length=HOP_GRID, trim=False,
                                           start_bpm=float(bpm_nominal), tightness=200)
        t = librosa.frames_to_time(beats, sr=sr22, hop_length=HOP_GRID)
        if len(t) < 32:
            return None, None, len(t)
        # Índice de beat esperado de cada detección, según el periodo nominal.
        periodo_nom = 60.0 / float(bpm_nominal)
        idx = np.round((t - t[0]) / periodo_nom)
        # Descartar detecciones que no caen cerca de una línea de beat (outliers)
        pred = t[0] + idx * periodo_nom
        ok = np.abs(t - pred) < periodo_nom * 0.25
        t, idx = t[ok], idx[ok]
        if len(t) < 32:
            return None, None, len(t)
        # Mínimos cuadrados: t = a*idx + b  →  a = periodo real
        A = np.vstack([idx, np.ones(len(idx))]).T
        a, b = np.linalg.lstsq(A, t, rcond=None)[0]
        if a <= 0:
            return None, None, len(t)
        bpm_real = 60.0 / a
        # Si se fue muy lejos del nominal, no es refinamiento: es otra detección.
        if abs(bpm_real - float(bpm_nominal)) > 1.5:
            return None, None, len(t)
        # Residuo ROBUSTO: percentil 90, no el máximo. Un solo beat mal
        # detectado disparaba el máximo y clasificaba como "variable" a
        # tracks perfectamente constantes (falso positivo medido en la prueba).
        errs = np.abs(t - (a * idx + b)) * 1000.0
        resid_ms = float(np.percentile(errs, 90))
        return round(bpm_real, 3), round(resid_ms, 1), int(len(t))
    except Exception:
        traceback.print_exc()
        return None, None, 0


def clasificar_tempo(resid_ms):
    """Tempo constante vs variable, por el residuo del ajuste lineal."""
    if resid_ms is None:
        return "desconocido"
    return "constante" if resid_ms <= TEMPO_RESID_MS else "variable"


def detect_mix_in(y22, sr22, bpm, first_beat_ms):
    """MIX-IN musical: primer downbeat de la primera frase con kick sostenido.

    ANTES (bug medido): 983/1005 tracks tenían el MIX-IN a <2 s — o sea marcaba
    el INICIO DEL AUDIO, no un punto de entrada de mezcla. Un DJ no lanza el
    track en el primer sample: lo lanza en la primera frase con groove estable.
    """
    try:
        beat_ms = 60000.0 / float(bpm)
        bar_ms = beat_ms * 4
        phrase_ms = bar_ms * 4          # frase de 4 compases como unidad de entrada
        # Energía de graves por compás (el kick)
        S = np.abs(librosa.stft(y22, n_fft=2048, hop_length=512))
        freqs = librosa.fft_frequencies(sr=sr22, n_fft=2048)
        bass = S[freqs < 200, :].sum(axis=0)
        times = librosa.frames_to_time(np.arange(len(bass)), sr=sr22, hop_length=512) * 1000.0
        dur_ms = (len(y22) / sr22) * 1000.0
        n_bars = int(max(0, (dur_ms - first_beat_ms) // bar_ms))
        if n_bars < 8:
            return None, False
        def bar_energy(b):
            t0 = first_beat_ms + b * bar_ms
            m = (times >= t0) & (times < t0 + bar_ms)
            return float(np.mean(bass[m])) if m.any() else 0.0
        e = _norm_max(np.array([bar_energy(b) for b in range(n_bars)]))
        umbral = 0.35 * float(np.max(e)) if np.max(e) > 0 else 0.0
        # Primera frase donde el kick cruza el umbral y SE SOSTIENE 4 compases
        for b in range(0, n_bars - 4):
            if all(e[b + k] >= umbral for k in range(4)):
                # snapear al inicio de frase
                bar_frase = int(round(b / 4.0) * 4)
                pos = first_beat_ms + bar_frase * bar_ms
                djfriendly = pos > (first_beat_ms + 2 * bar_ms)  # hubo intro real
                return int(round(pos)), bool(djfriendly)
        return int(round(first_beat_ms)), False
    except Exception:
        traceback.print_exc()
        return None, False


def detect_mix_out(y22, sr22, bpm, first_beat_ms):
    """MIX-OUT por SCORING GLOBAL (no voraz).

    ANTES (bug encontrado en el código v5): el bucle recorría de atrás hacia
    adelante y cortaba con `break` en la PRIMERA coincidencia. En un track con
    un breakdown profundo temprano, ese breakdown se confundía con el final:
    155 tracks quedaron con el MIX-OUT antes del 80% (casos extremos al 13%,
    saliendo del track a los 46 s de 354).

    AHORA: (1) se generan TODOS los candidatos, (2) se descarta lo anterior al
    70% de la duración, (3) se exige que la caída SE SOSTENGA hasta el final
    (que no reentre energía plena), (4) se elige por score global, no el primero.
    """
    try:
        beat_ms = 60000.0 / float(bpm)
        bar_ms = beat_ms * 4
        S = np.abs(librosa.stft(y22, n_fft=2048, hop_length=512))
        rms = librosa.feature.rms(S=S)[0]
        times = librosa.frames_to_time(np.arange(len(rms)), sr=sr22, hop_length=512) * 1000.0
        dur_ms = (len(y22) / sr22) * 1000.0
        n_bars = int(max(0, (dur_ms - first_beat_ms) // bar_ms))
        if n_bars < 16:
            return None, False
        def win(b0, b1):
            t0 = first_beat_ms + b0 * bar_ms
            t1 = first_beat_ms + b1 * bar_ms
            m = (times >= t0) & (times < t1)
            return float(np.mean(rms[m])) if m.any() else 0.0
        e = _norm_max(np.array([win(b, b + 1) for b in range(n_bars)]))
        bar_min = int(n_bars * MIXOUT_MIN_PCT)          # (2) piso de posición
        bar_max = n_bars - RUNWAY_BARS_MIN              # (garantía de runway)
        candidatos = []
        for b in range(bar_min, max(bar_min + 1, bar_max), 4):
            antes = float(np.mean(e[max(0, b - 4):b])) if b >= 4 else 0.0
            despues = float(np.mean(e[b:n_bars]))       # (3) toda la cola
            final = float(np.mean(e[max(b, n_bars - 8):n_bars]))
            caida = antes - despues
            sostiene = (final <= despues + 0.10)        # no reentra energía plena
            if antes >= 0.45 and caida > 0.10 and sostiene:
                runway_bars = n_bars - b
                score = caida * 1.0 + min(runway_bars / 32.0, 1.0) * 0.3
                candidatos.append((score, b))
        if candidatos:
            candidatos.sort(reverse=True)               # (4) el mejor, no el primero
            b = candidatos[0][1]
            return int(round(first_beat_ms + b * bar_ms)), True
        # Fallback honesto: mediana medida del catálogo, respetando runway
        b = min(int(round(n_bars * 0.87)), bar_max)
        b = max(b, bar_min)
        return int(round(first_beat_ms + b * bar_ms)), False
    except Exception:
        traceback.print_exc()
        return None, False


def compute_section_energy(y22, sr22, cues, dur_ms):
    """energy_entry / energy_peak / energy_exit (1-9) a partir de los cues.

    El campo `energy` global del catálogo sólo toma valores 7/8/9 (no
    discrimina). La energía POR SECCIÓN sí (rango medido 2-8), y es la que
    permite encadenar: la salida de un track debe casar con la entrada del
    siguiente.
    """
    try:
        rms = librosa.feature.rms(y=y22, hop_length=512)[0]
        times = librosa.frames_to_time(np.arange(len(rms)), sr=sr22, hop_length=512) * 1000.0
        def seg(t0, t1):
            m = (times >= t0) & (times < t1)
            return float(np.mean(rms[m])) if m.any() else 0.0
        pos = {c["label"]: c["positionMs"] for c in (cues or [])}
        mix_in = pos.get("MIX-IN", 0)
        mix_out = pos.get("MIX-OUT", dur_ms * 0.87)
        drop = pos.get("DROP 1", (mix_in + mix_out) / 2)
        vals = {
            "entry": seg(mix_in, mix_in + 30000),
            "peak": seg(drop, drop + 30000),
            "exit": seg(max(0, mix_out - 30000), mix_out),
        }
        pico = max(vals.values()) or 1.0
        # Escala 1-9 relativa al propio track (el ranking global lo hace la app)
        return {k: int(max(1, min(9, round(1 + 8 * (v / pico))))) for k, v in vals.items()}
    except Exception:
        traceback.print_exc()
        return {}


def sanity_check(result, dur_ms):
    """Validación automática. Devuelve (lista_de_problemas, confianza 0-1)."""
    problemas = []
    cues = result.get("cue_points") or []
    pos = {c["label"]: c["positionMs"] for c in cues}
    # Mirar el BPM que realmente se va a usar (el refinado desde el nominal
    # protegido), NO el que detect_grid estima por su cuenta: ese puede traer
    # error de octava (se midió 164 en un track de 123) y se descarta igual.
    bpm = result.get("bpm_precise") or result.get("bpm")
    if bpm and not (100 <= float(bpm) <= 150):
        problemas.append(f"bpm_fuera_de_rango:{bpm}")
    mi, mo = pos.get("MIX-IN"), pos.get("MIX-OUT")
    if mi is not None and dur_ms:
        pct = mi / dur_ms
        if pct > 0.15:
            problemas.append(f"mixin_tarde:{pct:.2f}")
    if mo is not None and dur_ms:
        pct = mo / dur_ms
        if pct < 0.80:
            problemas.append(f"mixout_temprano:{pct:.2f}")
        runway_s = (dur_ms - mo) / 1000.0
        if runway_s < 20:
            problemas.append(f"runway_corto:{runway_s:.0f}s")
    if mi is not None and mo is not None and mo <= mi:
        problemas.append("orden_invertido")
    if result.get("tempo_stability") == "variable":
        problemas.append("tempo_variable")
    confianza = max(0.0, 1.0 - 0.2 * len(problemas))
    return problemas, round(confianza, 2)


class AudioMudo(Exception):
    """La pista no tiene audio util (silencio): no hay tempo, rejilla ni cues que medir."""


SILENCIO_PICO = 1e-4      # ~ -80 dBFS
# Tope de un TRABAJO completo (descarga + analisis + CM2 + rendicion + master + subidas).
# Tiene que quedar por debajo del plazo de claim_analysis_job (8 min): si no, con
# varias replicas un tema lento lo toma otra replica a la vez (doble trabajo).
TOPE_TRABAJO_S = int(float(os.environ.get("TOPE_TRABAJO_S", "420")))
# Tope de descarga del original (MB): se baja por partes a disco, nunca entero a RAM.
MAX_TRACK_MB = int(float(os.environ.get("MAX_TRACK_MB", "250")))


def entero_js(x: float) -> int:
    """Math.round de JavaScript: ,5 hacia arriba (worker-result redondea así el bpm)."""
    return int(math.floor(float(x) + 0.5))


def analyze(path: str, bpm_seed=None) -> dict:
    y, sr = librosa.load(path, sr=SR, mono=True, duration=MAX_DURATION)
    if y.size == 0:
        raise RuntimeError("audio vacío")
    if float(np.max(np.abs(y))) < SILENCIO_PICO:
        raise AudioMudo("pista sin audio útil (silencio)")
    # ── Rejilla de compases — metodología derivada de Rekordbox (v5) ──
    # Reemplaza beat_track, que tomaba el PRIMER golpe detectado como
    # ancla (podía ser el 2, 3 o 4 del compás). Medido contra 729
    # rejillas reales de Rekordbox: 114 ms de error medio.
    # El v5 busca el ancla solo dentro del primer beat del archivo,
    # que es donde Rekordbox la pone en 729/729 casos.
    try:
        bpm, first_beat_ms = detect_grid(y, sr, seed_bpm=None)
        if not (40 < bpm < 240):
            bpm, first_beat_ms = None, None
    except Exception:
        traceback.print_exc()
        # Respaldo: el método anterior. Nunca quedarse sin dato.
        tempo, beats = librosa.beat.beat_track(y=y, sr=sr, trim=False)
        beat_times = librosa.frames_to_time(beats, sr=sr)
        # tempo puede venir como array de numpy (deprecación de float(ndarray)); tomamos el escalar.
        tempo_val = float(np.atleast_1d(tempo)[0]) if tempo is not None and np.atleast_1d(tempo).size else 0.0
        bpm = round(tempo_val, 2) if 40 < tempo_val < 240 else None
        first_beat_ms = int(round(float(beat_times[0]) * 1000)) if len(beat_times) else None

    peaks = bucket_reduce(y, BUCKETS, "peak")
    rms = bucket_reduce(y, BUCKETS, "rms")
    bands = compute_bands(y, sr, BUCKETS)
    rms_full = librosa.feature.rms(y=y)[0]
    energy = compute_energy(rms_full, bands)
    key, camelot = detect_key(y, sr)
    # Los cues NO se calculan aca: dependen del ANCLA, que se mide mas abajo
    # (compute_anchor). Una version previa los calculaba en este punto con la
    # rejilla vieja y despues el ancla cambiaba, dejando los cues cuantizados
    # contra una rejilla que ya no existia. Medido sobre 40 tracks reales: solo
    # el 8% quedaba en la grilla de frase.
    cues = None
    vocal_segments = detect_vocal_segments(y, sr, bpm, first_beat_ms)

    out = {
        "bpm": bpm,
        "key": camelot,          # guardamos Camelot (como el resto de la app)
        "first_beat_offset_ms": first_beat_ms,
        "waveform_peaks": peaks,
        "waveform_rms": rms,
        "waveform_bands": bands,
        "cue_points": cues,
        "vocal_segments": vocal_segments,
        "energy": energy,
    }

    # ── v7: pipeline "listo para mezclar" ────────────────────────────────────
    # Se corre a 22050 Hz (el análisis general va a 11025, que da ~46 ms de
    # frame — insuficiente para tempo decimal y para ubicar MIX-IN/MIX-OUT).
    try:
        bpm_ref = float(bpm_seed) if bpm_seed else (float(bpm) if bpm else None)
        if bpm_ref and 40 < bpm_ref < 240:
            y22, sr22 = librosa.load(path, sr=SR_GRID, mono=True, duration=MAX_DURATION)
            dur_ms = (len(y22) / sr22) * 1000.0

            # 1) BPM decimal (causa raíz de la deriva)
            bpm_fino, resid_ms, n_beats = refine_bpm(y22, sr22, bpm_ref)
            if bpm_fino:
                out["bpm_precise"] = bpm_fino
                # bpm_fine va contra el MISMO entero que usa worker-result. Sin semilla, ese
                # entero es Math.round(bpm) de JS (redondea ,5 hacia arriba; el round de
                # Python redondea al par: 124,5 daba 124 aquí y 125 allá, un BPM corrido).
                # Con semilla (solo llega con el BPM bloqueado), worker-result no toca `bpm`
                # y usa bpm_precise; `bpm` sigue siendo la medida propia de detect_grid,
                # que es la que audita la etiqueta (bpm_detected / bpm_etiqueta_difiere).
                out["bpm_fine"] = round(bpm_fino - entero_js(bpm_ref), 3)
                out["tempo_residual_ms"] = resid_ms
                out["tempo_stability"] = clasificar_tempo(resid_ms)
                print(f"    v7 bpm {bpm_ref} → {bpm_fino} (resid {resid_ms} ms, {n_beats} beats, {out['tempo_stability']})", flush=True)
                bpm_grid = bpm_fino
            else:
                out["tempo_stability"] = "desconocido"
                bpm_grid = bpm_ref

            # ANCLA: debe ser LA MISMA que usa el mixer (first_beat_detected_ms,
            # calculada sobre la rendition). Si se usa otra, los cues quedan
            # cuantizados contra una rejilla distinta a la que suena. Bug real
            # detectado en la prueba de 1 track: MIX-IN cayó a 44 ms.
            try:
                anc = compute_anchor(path, bpm_grid)
                fb = float(anc["ancla_ms"])
                out["first_beat_detected_ms"] = int(round(fb))
                out["grid_confidence"] = anc.get("confianza")
                print(f"    v7.2 ancla={fb:.0f} ms (residuo {anc['residuo_ms']} ms, conf {anc.get('confianza')}, kicks {anc.get('n_kicks')})", flush=True)
            except Exception:
                fb = float(first_beat_ms or 0)
                print("    v7 ancla: fallback a la rejilla interna", flush=True)

            # AHORA si: los cues se calculan sobre el ancla DEFINITIVA.
            cues = detect_cues(y, sr, bpm_grid, fb)
            if not cues:
                # Plan B: sin estructura medible, rejilla de frases.
                cues = cues_respaldo(y, sr, bpm_grid, fb)
                print(f"    v7.5 plan B de cues (rejilla): {len(cues or [])} cues", flush=True)
            out["cue_points"] = cues

            # 2-3) MIX-IN/MIX-OUT v7.1: REFUTADOS en el lote de 25 (20-ago) —
            # MIX-IN roto en tracks dinámicos, MIX-OUT empeoró el conjunto.
            # Quedan detrás de ENABLE_MIX_V7=1 (default APAGADO). La colocación
            # heredada de detect_cues se mantiene.
            mi = mo = None
            if ENABLE_MIX_V7:
                mi, djfriendly = detect_mix_in(y22, sr22, bpm_grid, fb)
                mo, mo_detectado = detect_mix_out(y22, sr22, bpm_grid, fb)
                out["intro_djfriendly"] = djfriendly
                out["mixout_detected"] = mo_detectado

            # 4) v7.2 — TODOS los cues se cuantizan SIEMPRE a la rejilla nueva
            # (ancla de ataque + BPM fino). Antes esto solo corría si MIX-IN/OUT
            # validaban, y si no, los cues quedaban pegados a la rejilla vieja:
            # exactamente el bug de "Oui" (8 hot cues a -47 ms de su propia
            # rejilla) que hacía saltar los hot cues a otro lado en el mixer.
            if cues:
                bar_ms = (60000.0 / bpm_grid) * 4
                beat_ms = 60000.0 / bpm_grid
                nuevos = []
                for c in cues:
                    c2 = dict(c)
                    if ENABLE_MIX_V7 and mi is not None and mo is not None and mo > mi:
                        if c2.get("label") == "MIX-IN":
                            c2["positionMs"] = int(mi)
                        elif c2.get("label") == "MIX-OUT":
                            c2["positionMs"] = int(mo)
                    nuevos.append(c2)
                for c2 in nuevos:
                    # El cue A va SIEMPRE al segundo 0 del archivo (metodologia
                    # MIK: es el punto de carga). Esta re-cuantizacion lo movia
                    # al downbeat mas cercano (medido: 0 -> 907 ms).
                    if int(c2.get("number", -1)) == 0:
                        c2["positionMs"] = 0
                        continue
                    p = c2["positionMs"]
                    # MIX-IN/OUT al compás; el resto de los cues al BEAT (los
                    # hot cues intermedios pueden legítimamente caer a mitad
                    # de compás — cuantizarlos a compás los movería de lugar).
                    paso = bar_ms if c2.get("label") in ("MIX-IN", "MIX-OUT") else beat_ms
                    q = fb + round((p - fb) / paso) * paso
                    q = max(0, min(q, dur_ms - 1000))
                    c2["positionMs"] = int(round(q))
                # 5) Descartar cues duplicados tras cuantizar
                vistos, limpios = set(), []
                for c2 in sorted(nuevos, key=lambda x: x["positionMs"]):
                    if c2["positionMs"] in vistos:
                        continue
                    vistos.add(c2["positionMs"])
                    limpios.append(c2)
                out["cue_points"] = limpios
                cues = limpios

            # 6) Energía por sección (base del arco de los sets)
            se = compute_section_energy(y22, sr22, cues, dur_ms)
            if se:
                out["energy_entry"] = se["entry"]
                out["energy_peak"] = se["peak"]
                out["energy_exit"] = se["exit"]

            # 7) Validación automática + score de confianza
            problemas, confianza = sanity_check(out, dur_ms)
            out["analysis_confidence"] = confianza
            if problemas:
                out["analysis_flags"] = problemas
                print(f"    v7 ⚠ {', '.join(problemas)} (confianza {confianza})", flush=True)
    except Exception:
        traceback.print_exc()
        print("    v7 falló (no bloquea el job)", flush=True)

    dur_total_ms = (len(y) / sr) * 1000.0
    if not out.get("cue_points"):
        # Plan B sin el pipeline v7 (BPM de detect_grid) o Plan C (sin BPM).
        out["cue_points"] = cues_respaldo(y, sr, bpm, first_beat_ms) or cues_por_tiempo(dur_total_ms)
        print(f"    v7.5 respaldo de cues: {len(out['cue_points'])} ({out['cue_points'][0].get('origen')})", flush=True)
    # Duracion real (solo si no se corto en MAX_DURATION): muchos temas generados llegan sin ella.
    if dur_total_ms < (MAX_DURATION - 1) * 1000:
        out["duration_seconds"] = int(round(dur_total_ms / 1000.0))
    return out


# ----------------------------------------------------------------------------
# Fase 2 — Identificación por huella acústica (Chromaprint + AcoustID)
# Best-effort: si no hay ACOUSTID_API_KEY o falta `fpcalc`, devuelve None sin romper.
# ----------------------------------------------------------------------------
def fingerprint_identify(path: str):
    """Devuelve {'artist','title'} identificando la canción por huella, o None."""
    if not ACOUSTID_API_KEY:
        return None
    try:
        proc = subprocess.run(
            ["fpcalc", "-json", path],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0 or not proc.stdout:
            return None
        fp = json.loads(proc.stdout)
        fingerprint = fp.get("fingerprint")
        duration = int(round(float(fp.get("duration", 0) or 0)))
        if not fingerprint or duration <= 0:
            return None
        r = requests.get(
            "https://api.acoustid.org/v2/lookup",
            params={
                "client": ACOUSTID_API_KEY,
                "meta": "recordings",
                "duration": duration,
                "fingerprint": fingerprint,
            },
            headers={"User-Agent": "DeepMancho/1.0 ( https://deepmancho.com )"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        # Ordena por score y toma el mejor recording con artista+título.
        results = sorted(data.get("results") or [], key=lambda x: x.get("score", 0), reverse=True)
        for res in results:
            for rec in (res.get("recordings") or []):
                title = (rec.get("title") or "").strip()
                artists = rec.get("artists") or []
                artist = (artists[0].get("name") or "").strip() if artists else ""
                if title and artist:
                    return {"artist": artist, "title": title}
        return None
    except FileNotFoundError:
        # `fpcalc` no instalado en la imagen → deshabilitar silenciosamente.
        print("WARN: fpcalc no encontrado; identificación por huella deshabilitada", flush=True)
        return None
    except Exception:
        return None


# ----------------------------------------------------------------------------
# CM1-bis — Loudness (LUFS integrado, BS.1770 vía pyloudnorm)
# ----------------------------------------------------------------------------
def compute_loudness_lufs(path: str):
    """LUFS integrado del archivo. None si pyloudnorm no está o el audio falla.
    Nunca rompe el job: la ausencia de loudness no debe frenar el análisis."""
    return medir_sonoridad(path).get("loudness_lufs")


def medir_sonoridad(path: str) -> dict:
    """`loudness_lufs` (BS.1770 en ESTÉREO, 7.6.9) y `energy_v2` (#248).

    Hasta la 7.6.8, `loudness_lufs` se medía sobre la mezcla en MONO: daba 3–4 dB menos
    que cualquier medidor estándar (Weekend's Started: −12,2 «mono» contra −8,4 LUFS
    reales). No era que el MP3 sonara más fuerte; era otra escala (#320). Los objetivos de
    normalización de la app y de la radio (−12,5) estaban calibrados en la escala mono:
    pasan a ≈ −9 en el mismo despliegue (ver CHANGELOG 7.6.9).

    energy_v2 es la energía 1-10 sin saturar de analizador_v8 (LUFS estéreo de -20 a
    -6, agudos absolutos y golpes por segundo). Va como CAMPO APARTE: `energy` no
    cambia hasta calibrarla con la referencia del dueño. Con las dos en el catálogo
    real se comparan antes de decidir. Se mide sobre la misma carga a 44,1 kHz que
    ya se hacía para el LUFS (en estéreo: el doble de RAM durante este paso)."""
    try:
        import pyloudnorm  # dependencia: pyloudnorm>=0.1 (requirements)
    except ImportError:
        print("WARN CM1: pyloudnorm no instalado; loudness_lufs no se calcula", flush=True)
        return {}
    out = {}
    try:
        y, sr44 = librosa.load(path, sr=44100, mono=False, duration=MAX_DURATION)
        y = np.atleast_2d(y)
        if y.size == 0:
            return {}
        mono = y.mean(axis=0)  # lo mismo que librosa.load(mono=True)
        meter = pyloudnorm.Meter(sr44)
        import analizador_v8 as v8
        estereo = np.ascontiguousarray(v8._estereo(y.T))
        lufs = float(meter.integrated_loudness(estereo))       # BS.1770: L + R
        if np.isfinite(lufs):
            out["loudness_lufs"] = round(lufs, 2)
            lufs_mono = float(meter.integrated_loudness(mono))
            print(f"    LUFS {lufs:.1f} (escala mono de antes: {lufs_mono:.1f})", flush=True)
        try:
            mezcla = {"lufs_integrado": v8._r(lufs, 1) if np.isfinite(lufs) else None,
                      "tercios_db": v8.tercios_de_octava(mono, sr44)}
            golpes = librosa.onset.onset_detect(y=mono, sr=sr44, units="time")
            dur_s = len(mono) / sr44
            e2 = v8.energia_v2(mezcla, len(golpes) / dur_s if dur_s > 0 else None)
            if e2 is not None:
                out["energy_v2"] = e2
        except Exception as e:
            print(f"WARN energy_v2 (no bloquea): {e}", flush=True)
        return out
    except Exception:
        traceback.print_exc()
        return out


# ----------------------------------------------------------------------------
# CM2 — Ancla de rejilla de precisión (fase de beat + downbeat) — ver nota arriba
# ----------------------------------------------------------------------------
def _fase_por_segmento(onset: np.ndarray, times: np.ndarray, periodo_s: float, segs: int = 12):
    """Fase circular (media ponderada por energía) del peine de beats, por segmento."""
    n = len(onset)
    borde = np.linspace(0, n, segs + 1).astype(int)
    pts = []
    for s in range(segs):
        i0, i1 = borde[s], borde[s + 1]
        w = onset[i0:i1]
        if w.sum() < 1e-9:
            continue
        ang = 2.0 * np.pi * (times[i0:i1] % periodo_s) / periodo_s
        z = np.sum(w * np.exp(1j * ang))
        if abs(z) < 1e-9:
            continue
        pts.append((float(times[i0:i1].mean()), float(np.angle(z)), float(abs(z))))
    return pts


def _ajuste_lineal_fase(pts, periodo_s: float):
    """Desenrolla la fase entre segmentos y ajusta fase(t)=a·t+b (ponderado).
    a corrige la micro-desviación de tempo; b es la fase absoluta en t=0.
    Devuelve (frecuencia_real_hz, fase_b, residuo_max_ms)."""
    if len(pts) < 3:
        raise RuntimeError("CM2: muy pocos segmentos con energía para ajustar fase")
    T = np.array([p[0] for p in pts]); PH = np.array([p[1] for p in pts]); W = np.array([p[2] for p in pts])
    des = PH.copy()
    for k in range(1, len(des)):
        while des[k] - des[k - 1] > np.pi:  des[k] -= 2 * np.pi
        while des[k] - des[k - 1] < -np.pi: des[k] += 2 * np.pi
    sw, st = W.sum(), (W * T).sum()
    stt, sp, stp = (W * T * T).sum(), (W * des).sum(), (W * T * des).sum()
    a = (sw * stp - st * sp) / (sw * stt - st * st)
    b = (sp - a * st) / sw
    resid_ms = float(np.max(np.abs(des - (a * T + b))) / (2 * np.pi) * periodo_s * 1000)
    return (1.0 / periodo_s) + a / (2 * np.pi), b, resid_ms


def compute_anchor(path: str, bpm: float):
    """v7.2 — Ancla de rejilla anclada al ATAQUE del kick (no al pico de envolvente).

    Por qué (evidencia del golden set, 21-ago): 9/24 tracks del catálogo tenían
    el ancla corrida -77..-69 ms con rejilla y BPM perfectos. Causa: la onset
    envelope de librosa reacciona tarde/temprano respecto del ataque perceptual
    del bombo, que es lo que un DJ (y Rekordbox) usa como beat. Método
    certificado en la auditoría: banda de kick 35-130 Hz (Butterworth) +
    envolvente de Hilbert + tiempo de ataque en el cruce del 25% de la altura
    del pico. Cambios v7.2 vs v6.1:
      * Eventos discretos de ataque de kick (no la envolvente continua).
      * Desambiguación del downbeat con la banda de caja/clap (1.5-5 kHz):
        en 4x4 el snare cae en 2 y 4 — resuelve el corrimiento de 1-3 beats.
      * grid_confidence (0-1) reportado para que la app sepa cuánto fiarse.
    El BPM de entrada sigue siendo fuente de verdad (solo ±0.05 de grilla fina).
    Devuelve dict(ancla_ms, bpm_real, residuo_ms, confianza, n_kicks)."""
    from scipy.signal import butter, sosfiltfilt, hilbert, find_peaks

    y, sr = librosa.load(path, sr=ANCHOR_SR, mono=True, duration=MAX_DURATION)
    if y.size == 0:
        raise RuntimeError("CM2: audio vacío")
    periodo_nom = 60.0 / float(bpm)

    # --- Envolvente de la banda de kick (35-130 Hz) ---
    sos = butter(4, [35.0, 130.0], btype="band", fs=sr, output="sos")
    yb = sosfiltfilt(sos, y.astype(np.float64))
    env = np.abs(hilbert(yb)).astype(np.float32)
    w_sm = max(1, int(0.005 * sr))  # suavizado ~5 ms
    env = np.convolve(env, np.ones(w_sm, dtype=np.float32) / w_sm, mode="same")

    # --- Eventos de kick: picos separados al menos ~0.45 del periodo ---
    p99 = float(np.percentile(env, 99))
    if p99 <= 0:
        raise RuntimeError("CM2: sin energía de graves")
    pk, props = find_peaks(env, distance=max(1, int(0.45 * periodo_nom * sr)),
                           height=0.30 * p99)
    if len(pk) < 24:
        raise RuntimeError(f"CM2: muy pocos kicks detectados ({len(pk)})")

    # --- ATAQUE de cada kick: último cruce del 25% de SU pico, hacia atrás ---
    lim_atras = int(0.150 * sr)  # un ataque real no dura más de 150 ms
    ataques = np.empty(len(pk)); subidas = np.empty(len(pk))
    pesos = props["peak_heights"].astype(np.float64)
    for i, p in enumerate(pk):
        th = 0.25 * env[p]
        j, lo = p, max(0, p - lim_atras)
        while j > lo and env[j] > th:
            j -= 1
        ataques[i] = j / sr
        subidas[i] = (p - j) / sr * 1000.0     # ms de ataque: el kick es seco

    # --- Grilla fina de tempo (BPM protegido ±0.05): histograma plegado ---
    NBINS = 256

    def hist_plegado(ts, ws, periodo_s):
        b = np.floor(((ts % periodo_s) / periodo_s) * NBINS).astype(int) % NBINS
        H = np.bincount(b, weights=ws, minlength=NBINS)
        return (np.roll(H, 1) + H + np.roll(H, -1)) / 3.0

    def pico_interp(H, periodo_s):
        k = int(np.argmax(H))
        a, c = H[(k - 1) % NBINS], H[(k + 1) % NBINS]
        den = (a - 2 * H[k] + c)
        delta = 0.5 * (a - c) / den if abs(den) > 1e-12 else 0.0
        return ((k + delta) / NBINS) * periodo_s % periodo_s

    mejor = None
    for dbpm in np.linspace(-0.05, 0.05, 21):
        p = 60.0 / (float(bpm) + dbpm)
        H = hist_plegado(ataques, pesos, p)
        nitidez = float(H.max() / (H.mean() + 1e-12))
        if mejor is None or nitidez > mejor[0]:
            mejor = (nitidez, p, H)
    nitidez, periodo_real, H = mejor
    t_beat0 = pico_interp(H, periodo_real)

    # NOTA (25-ago-2026): aca se probo una DESAMBIGUACION DE FASE (v7.5) que
    # elegia entre 4 fases candidatas la de menor "residuo de inliers + 0.6 x
    # tiempo de ataque". FUE REFUTADA con medicion sobre 80 tracks de audio real
    # con arbitro ciego comun: cambio la fase en 23 tracks y EMPEORO 19 de ellos.
    #   p90 <= 20 ms : 80.0% (esta version) -> 67.5% (con seleccion)
    #   p90 <= 10 ms : 61.3% -> 48.8%
    #   SD del offset entre canciones: 3.26 ms -> 18.69 ms (5.7x peor)
    # Causa: cada fase candidata armaba su PROPIO conjunto de inliers, asi que
    # podia ganar una fase con pocos golpes secos aunque representara peor al
    # tren global. Si alguna vez se reintenta: evaluar los candidatos contra un
    # soporte GLOBAL congelado y exigir una ventaja minima antes de abandonar
    # el pico del histograma. Ver docs/validacion-fase-v75.md.

    # --- Residuo: dispersión del pico por segmentos (solo segmentos con kicks) ---
    SEGS = 8
    borde = np.linspace(ataques.min(), ataques.max() + 1e-6, SEGS + 1)
    desvios = []
    for s in range(SEGS):
        m = (ataques >= borde[s]) & (ataques < borde[s + 1])
        if pesos[m].sum() < 0.02 * pesos.sum():
            continue
        Hs = hist_plegado(ataques[m], pesos[m], periodo_real)
        ts = pico_interp(Hs, periodo_real)
        d = (ts - t_beat0) % periodo_real
        if d > periodo_real / 2:
            d -= periodo_real
        desvios.append(abs(d))
    resid_ms = float(np.median(desvios) * 1000.0) if desvios else 999.0

    # --- Downbeat: kicks por slot + SNARE (1.5-5 kHz) en 2 y 4 ---
    # Solo votan COMPASES COMPLETOS: el compás truncado del arranque/final
    # mete un kick de más en un slot y volcaba el empate 1-vs-3 para el lado
    # equivocado (refutado con la señal sintética que arranca en el beat 3).
    # Limitación documentada: si el patrón es simétrico (snare idéntico en 2 y
    # 4, kicks parejos), beat 1 y beat 3 son indistinguibles desde la señal —
    # la paridad elegida sigue siendo beat-compatible para la mezcla.
    compas = 4.0 * periodo_real
    t_lo = ataques.min() + compas
    t_hi = ataques.max() - compas
    m_full = (ataques >= t_lo) & (ataques <= t_hi)
    at_v, pe_v = (ataques[m_full], pesos[m_full]) if m_full.sum() >= 16 else (ataques, pesos)
    slot_k = np.floor(((at_v - t_beat0) % compas) / periodo_real).astype(int) % 4
    kick_slot = np.array([pe_v[slot_k == k].sum() for k in range(4)])

    snare_slot = np.zeros(4)
    try:
        sos_s = butter(4, [1500.0, 5000.0], btype="band", fs=sr, output="sos")
        ys = sosfiltfilt(sos_s, y.astype(np.float64))
        env_s = np.abs(ys).astype(np.float32)
        env_s = np.convolve(env_s, np.ones(w_sm, dtype=np.float32) / w_sm, mode="same")
        pk_s, pr_s = find_peaks(env_s, distance=max(1, int(0.45 * periodo_real * sr)),
                                height=0.30 * float(np.percentile(env_s, 99)))
        if len(pk_s) >= 16:
            t_s = pk_s / sr
            h_s = pr_s["peak_heights"]
            m_s = (t_s >= t_lo) & (t_s <= t_hi)
            if m_s.sum() >= 8:
                t_s, h_s = t_s[m_s], h_s[m_s]
            sl = np.floor(((t_s - t_beat0) % compas) / periodo_real).astype(int) % 4
            snare_slot = np.array([h_s[sl == k].sum() for k in range(4)])
    except Exception:
        pass

    kn = kick_slot / (kick_slot.sum() + 1e-12)
    sn = snare_slot / (snare_slot.sum() + 1e-12)
    if snare_slot.sum() > 0:
        # score del candidato a downbeat k: snare fuerte en (k+1) y (k+3), kick en k
        score = np.array([sn[(k + 1) % 4] + sn[(k + 3) % 4] + 0.5 * kn[k] for k in range(4)])
    else:
        score = kn.copy()
    down = int(np.argmax(score))
    # Empate 1-vs-3 (snare en 2y4 es simétrico ante un corrimiento de 2 beats):
    # desempatar por la paridad cuyo downbeat cae MAS TEMPRANO en el audio.
    # Los intros de DJ arrancan en el beat 1 en la gran mayoría del catálogo;
    # si el track de verdad arranca en el 3, el error queda a nivel de paridad
    # de compás (beat-compatible), nunca a nivel de beat.
    alt = (down + 2) % 4
    if score[down] - score[alt] < 0.05 * (score[down] + 1e-12):
        t_ini_ = float(ataques.min())
        def _primer_ancla(d):
            td = (t_beat0 + d * periodo_real) % compas
            kk = math.ceil((t_ini_ - 0.6 * periodo_real - td) / compas)
            a = td + kk * compas
            while a < 0:
                a += compas
            return a
        if _primer_ancla(alt) < _primer_ancla(down) - 1e-6:
            down = alt
    t_down0 = (t_beat0 + down * periodo_real) % compas

    # --- Ancla = downbeat de la rejilla del primer compás con kick ---
    # Tolerancia de media negra: el ataque detectado del primer kick puede
    # caer unos ms antes O después del tiempo exacto de rejilla; con una
    # tolerancia de 1 ms un jitter de +5 ms saltaba un compás entero
    # (refutado con la señal sintética de verdad conocida).
    t_inicio = float(ataques.min())
    k = math.ceil((t_inicio - 0.6 * periodo_real - t_down0) / compas)
    ancla_s = t_down0 + k * compas
    while ancla_s < 0:
        ancla_s += compas

    # --- Confianza de rejilla (0-1): nitidez del pico + estabilidad + soporte ---
    conf = min(1.0, nitidez / 8.0) * max(0.0, 1.0 - min(resid_ms, 40.0) / 40.0)
    conf *= min(1.0, len(pk) / 120.0)
    return dict(ancla_ms=round(float(ancla_s) * 1000.0, 1),
                bpm_real=round(60.0 / float(periodo_real), 3),
                residuo_ms=round(float(resid_ms), 1),
                confianza=round(float(conf), 2),
                n_kicks=int(len(pk)))


def rendition_url(track_id: str, formato: str = "aac") -> str:
    """URL del MISMO audio que reproduce el navegador (stream-track)."""
    return f"{WORKER_API_URL}/stream-track?track_id={track_id}&format={formato}"


def descargar_rendicion(track_id: str) -> str:
    """Baja la rendition de stream-track a un temporal y devuelve su ruta.
    Manda x-worker-secret: sin el, stream-track responde 401 (antes 404) y CM2
    fallaba en cada tema. La extension sale del content-type real (m4a o mp3).
    Si el m4a legado no esta en el Storage (stream-track da 502, issue #35 de la
    plataforma), pide la otra rendicion del mismo tema: el MP3."""
    cab = {"x-worker-secret": WORKER_SECRET}
    r = requests.get(rendition_url(track_id), headers=cab, timeout=180)
    if r.status_code >= 500:
        r = requests.get(rendition_url(track_id, "mp3"), headers=cab, timeout=180)
    r.raise_for_status()
    suf = ".m4a" if "mp4" in r.headers.get("content-type", "") else ".mp3"
    tmp = tempfile.NamedTemporaryFile(suffix=suf, delete=False)
    tmp.write(r.content)
    tmp.close()
    return tmp.name


def ancla_de_rendicion(track_id: str, bpm: float) -> dict:
    """compute_anchor sobre la rendition; el temporal se borra aunque falle."""
    ruta = descargar_rendicion(track_id)
    try:
        return compute_anchor(ruta, bpm)
    finally:
        os.remove(ruta)


def _wrap(x: float, T: float) -> float:
    x = x % T
    return x - T if x > T / 2 else x


def ancla_golden(i: int) -> dict:
    """Genera el tema sintético i, lo pasa por make_rendition (el MP3 de escucha) y mide
    su ancla con compute_anchor. Los temporales se borran aunque falle."""
    import soundfile as sf
    nombre, bpm, fase, kick_hz, caida, bajo = GOLDEN_SINTETICO[i]
    wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    mp3 = None
    try:
        sf.write(wav, tema_golden(bpm, fase, kick_hz, caida, bajo, semilla=i), 44100)
        mp3 = make_rendition(wav)
        if not mp3:
            raise RuntimeError("make_rendition no generó el MP3")
        return compute_anchor(mp3, bpm)
    finally:
        for f in (wav, mp3):
            if f and os.path.exists(f):
                os.remove(f)


def golden_exam():
    """Examen del golden set sintético (#573). Solo genera audio local y mide; NUNCA
    escribe en la base ni usa la red. Gate: |error relativo| <= ANCHOR_TOL_MS en los
    3 pares -> APROBADO. Además imprime el error absoluto de cada tema contra su oro."""
    print(f"[CM2 EXAMEN] arrancando examen del golden set sintético ({len(GOLDEN_SINTETICO)} temas, sin red)...", flush=True)
    resultados = {}
    for i, (title, bpm, gold, *_resto) in enumerate(GOLDEN_SINTETICO):
        try:
            res = ancla_golden(i)
            res["gold"] = gold; res["bpm"] = bpm
            resultados[i] = res
            T = 60000.0 / bpm
            abs_err = _wrap(float(res["ancla_ms"]) - gold, T)
            flag = " ⚠ residuo alto" if res["residuo_ms"] > 8 else ""
            print(f"[CM2 EXAMEN] {title}: ancla={res['ancla_ms']}ms (oro {gold} ms, error {abs_err:+.1f} ms) "
                  f"bpm_real={res['bpm_real']} residuo={res['residuo_ms']}ms{flag}", flush=True)
        except Exception as e:
            print(f"[CM2 EXAMEN] {title}: FALLO al analizar ({e})", flush=True)
    aprobado = True
    for a, b in GOLDEN_PAIRS:
        if a not in resultados or b not in resultados:
            print(f"[CM2 EXAMEN] Par {GOLDEN_SINTETICO[a][0]} × {GOLDEN_SINTETICO[b][0]}: SIN DATOS", flush=True)
            aprobado = False
            continue
        A, B = resultados[a], resultados[b]
        # Error de cada tema contra su oro, plegado a SU período, y después la resta: con el
        # mismo BPM da lo mismo que la fórmula de antes, y con BPM distintos sigue siendo
        # correcta (comparar anclas absolutas con un período promedio no lo era).
        eA = _wrap(float(A["ancla_ms"]) - A["gold"], 60000.0 / A["bpm"])
        eB = _wrap(float(B["ancla_ms"]) - B["gold"], 60000.0 / B["bpm"])
        err = eB - eA
        ok = abs(err) <= ANCHOR_TOL_MS
        aprobado = aprobado and ok
        print(f"[CM2 EXAMEN] Par {GOLDEN_SINTETICO[a][0]} × {GOLDEN_SINTETICO[b][0]}: "
              f"error {err:+.1f} ms {'✅' if ok else '❌'}", flush=True)
    print(f"[CM2 EXAMEN] RESULTADO: {'APROBADO ✅' if aprobado else 'NO APROBADO ❌'}"
          f" (criterio ±{ANCHOR_TOL_MS} ms por par)", flush=True)
    if aprobado and not ENABLE_ANCHOR_BACKFILL:
        print("[CM2 EXAMEN] Para habilitar el backfill de anclas: variable "
              "ENABLE_ANCHOR_BACKFILL=true (requiere worker-result con soporte "
              "de first_beat_detected_ms).", flush=True)
    return aprobado


# ----------------------------------------------------------------------------
# API (Edge Functions) helpers
# ----------------------------------------------------------------------------
def next_job():
    """Reclama el siguiente job y devuelve (job, track, audio_url) o (None, None, None)."""
    r = requests.post(f"{WORKER_API_URL}/worker-next", headers=HEADERS, timeout=30)
    if r.status_code == 401:
        raise RuntimeError("401: WORKER_SECRET incorrecto")
    r.raise_for_status()
    data = r.json()
    job = data.get("job")
    if not job:
        return None, None, None, None
    # El 4.º valor lleva las dos subidas firmadas: rendicion de escucha y master MP3 320
    # (este ultimo solo cuando la plataforma marca needs_master_conversion).
    return job, data.get("track") or {}, data.get("audio_url"), {
        "rendition": data.get("rendition_upload"), "master": data.get("master_upload")}


# ---------------------------------------------------------------------------
# FORMATO ESTANDAR DE LA PLATAFORMA (DJCONNECT_AUDIO_STANDARD v1, 25-ago-2026)
# MP3 (libmp3lame) CBR 192 kbps, 44.1 kHz, estereo. Documentado en
# docs/estrategia-almacenamiento.md y en el doc 08 del Project Knowledge.
# Por que MP3 y no AAC: el contenedor MP4 depende del atomo `moov` y ya produjo
# archivos corruptos -> DEMUXER_ERROR_NO_SUPPORTED_STREAMS y "Media failed to
# decode" en iOS Safari, medidos en produccion. MP3 no tiene contenedor fragil,
# decodifica en todo el parque (Safari/PWA, Web Audio, Liquidsoap) y en CBR da
# seek deterministico, que es lo que necesitan el beatgrid y los hot cues.
# NUNCA aplicar loudnorm/volume aca: `loudness_lufs` se mide sobre el audio y la
# normalizacion se aplica en REPRODUCCION. Normalizar en el archivo rompe esa
# medicion y puede introducir clipping en los picos de graves.
# ---------------------------------------------------------------------------
AUDIO_STANDARD = {
    "codec": "libmp3lame", "bitrate": "192k", "sample_rate": "44100",
    "channels": "2", "ext": ".mp3", "mime": "audio/mpeg",
}


# Master de biblioteca (7.5.1): todo lo que suben los DJs se guarda en MP3 320k;
# solo las canciones creadas en el Estudio conservan WAV para descargar.
MASTER_BITRATE = "320k"


# #265 (privacidad): la copia de ESCUCHA no lleva etiquetas (titulo, artista,
# album, comentarios ni portada): la radio y el catalogo publico la sirven a
# anonimos y el visitante nunca debe ver el nombre real. El master de DESCARGA
# (MP3 320k para el dueno o el comprador) si conserva sus etiquetas.
SIN_ETIQUETAS = ["-map_metadata", "-1", "-map_chapters", "-1", "-id3v2_version", "0",
                 "-write_id3v1", "0", "-fflags", "+bitexact"]


def make_rendition(src_path: str, bitrate: str = None, sufijo: str = ".stream", etiquetas: bool = False):
    """Convierte al ESTANDAR de la plataforma (MP3 CBR 192k, o `bitrate`). Ruta o None.
    `etiquetas=False` (escucha): sin metadatos ni portada. True: solo el master de descarga."""
    try:
        out = src_path + sufijo + AUDIO_STANDARD["ext"]
        meta = ["-map_metadata", "0", "-id3v2_version", "3"] if etiquetas else SIN_ETIQUETAS
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", src_path,
             "-map", "0:a:0", "-vn",        # solo el audio: la portada embebida no pasa
             *meta,
             "-write_xing", "1",            # header Xing: duracion y seek fiables
             "-ar", AUDIO_STANDARD["sample_rate"],
             "-ac", AUDIO_STANDARD["channels"],
             "-c:a", AUDIO_STANDARD["codec"],
             "-b:a", bitrate or AUDIO_STANDARD["bitrate"],
             "-f", "mp3", out],
            capture_output=True, timeout=180,
        )
        if proc.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
            return None
        return out
    except Exception:
        return None


def upload_rendition(signed_url: str, rendition_path: str) -> bool:
    """Sube la rendition estandar (MP3) a la URL firmada de Supabase Storage."""
    try:
        with open(rendition_path, "rb") as f:
            data = f.read()
        r = requests.put(
            signed_url,
            data=data,
            headers={"content-type": AUDIO_STANDARD["mime"], "x-upsert": "true"},
            timeout=180,
        )
        return r.status_code in (200, 201)
    except Exception:
        return False


# ----------------------------------------------------------------------------
# GENERO detectado (2-oct-2026, pedido del dueno: ~1.000 temas subidos sin ordenar;
# cada genero alimenta su emisora). Etapa A: la etiqueta del archivo (ID3 TCON,
# Beatport, Vorbis). Etapa B (clasificador por audio) llega con el catalogo etiquetado.
# No escribe en la base: va por worker-result como genre_detected / genre_confidence.
# ----------------------------------------------------------------------------
GENERO_UMBRAL_REVISAR = 0.7

# Nombres de Beatport (los que usa la Biblioteca) y sus variantes frecuentes.
_GENEROS_BEATPORT = {
    "tech house": "Tech House", "techhouse": "Tech House",
    "house": "House", "deep house": "Deep House", "afro house": "Afro House",
    "minimal / deep tech": "Minimal / Deep Tech", "minimal": "Minimal / Deep Tech", "deep tech": "Minimal / Deep Tech",
    "melodic house & techno": "Melodic House & Techno", "melodic house and techno": "Melodic House & Techno",
    "melodic techno": "Melodic House & Techno", "melodic house": "Melodic House & Techno",
    "techno": "Techno (Peak Time / Driving)", "techno (peak time / driving)": "Techno (Peak Time / Driving)",
    "peak time techno": "Techno (Peak Time / Driving)", "techno (raw / deep / hypnotic)": "Techno (Raw / Deep / Hypnotic)",
    "hard techno": "Hard Techno", "progressive house": "Progressive House", "progressive": "Progressive House",
    "organic house / downtempo": "Organic House / Downtempo", "organic house": "Organic House / Downtempo",
    "downtempo": "Organic House / Downtempo", "jackin house": "Jackin House", "funky house": "Jackin House",
    "nu disco / disco": "Nu Disco / Disco", "nu disco": "Nu Disco / Disco", "disco": "Nu Disco / Disco",
    "indie dance": "Indie Dance", "electro house": "Electro House", "bass house": "Bass House",
    "uk garage / bassline": "UK Garage / Bassline", "uk garage": "UK Garage / Bassline", "garage": "UK Garage / Bassline",
    "trance": "Trance (Main Floor)", "trance (main floor)": "Trance (Main Floor)", "psy-trance": "Psy-Trance",
    "psytrance": "Psy-Trance", "drum & bass": "Drum & Bass", "drum and bass": "Drum & Bass", "dnb": "Drum & Bass",
    "dubstep": "Dubstep", "breaks / breakbeat / uk bass": "Breaks / Breakbeat / UK Bass", "breaks": "Breaks / Breakbeat / UK Bass",
    "dance / electro pop": "Dance / Electro Pop", "dance / pop": "Dance / Electro Pop", "electro pop": "Dance / Electro Pop",
    "afro / latin": "Afro / Latin", "latin house": "Afro / Latin", "mainstage": "Mainstage", "big room": "Mainstage",
    "hip-hop": "Hip-Hop", "hip hop": "Hip-Hop", "reggaeton": "Reggaeton", "pop": "Pop", "r&b": "R&B",
}
# Demasiado amplios para elegir emisora: se envian pero quedan por revisar.
_GENEROS_AMPLIOS = {"electronic", "electronica", "electrónica", "dance", "edm", "club", "other", "otros",
                    "unknown", "desconocido", "various", "misc", "music", "genre", "general"}
# ID3v1 numerico mas comun en musica de club: (n) -> nombre.
_ID3V1 = {"13": "pop", "18": "techno", "26": "dance", "31": "trance", "35": "house", "52": "electronic",
          "98": "electronica", "127": "drum & bass"}


def leer_etiquetas(path: str) -> dict:
    """Etiquetas del contenedor (ID3, Vorbis, MP4) con ffmpeg, en minusculas."""
    try:
        p = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "ffmetadata", "-"],
                           capture_output=True, text=True, timeout=30)
    except Exception:
        return {}
    out = {}
    for linea in (p.stdout or "").splitlines():
        if "=" in linea and not linea.startswith(";"):
            k, v = linea.split("=", 1)
            out.setdefault(k.strip().lower(), v.strip())
    return out


def genero_de_etiqueta(crudo):
    """(genero, confianza) a partir de la etiqueta; (None, 0.0) si no hay nada util."""
    if not crudo:
        return None, 0.0
    texto = crudo.strip()
    m = re.fullmatch(r"\(?(\d{1,3})\)?", texto)
    if m:
        texto = _ID3V1.get(m.group(1), "")
    # Varios generos en una etiqueta ("Tech House; House"): el primero reconocido.
    partes = [x.strip() for x in re.split(r"[;|/,]\s*(?=[A-Za-z])|\x00", texto) if x.strip()] or [texto]
    for cand in [texto] + partes:
        clave = re.sub(r"\s+", " ", cand.lower()).strip()
        if clave in _GENEROS_BEATPORT:
            return _GENEROS_BEATPORT[clave], 0.95
    clave = re.sub(r"\s+", " ", texto.lower()).strip()
    if not clave or clave in _GENEROS_AMPLIOS:
        return (texto.title() if clave else None), (0.3 if clave else 0.0)
    return texto[:60], 0.6  # genero desconocido para nosotros: se respeta, por revisar


def detectar_genero(path: str) -> dict:
    """Bloque de genero para worker-result (etapa A: etiquetas)."""
    tags = leer_etiquetas(path)
    genero, conf = genero_de_etiqueta(tags.get("genre") or tags.get("tcon"))
    out = {"genre_detected": genero, "genre_confidence": round(conf, 2),
           "genre_source": "etiqueta" if genero else None}
    if conf < GENERO_UMBRAL_REVISAR:
        out["genero_por_revisar"] = True
    return out


class ArchivoMuyGrande(RuntimeError):
    """El original supera MAX_TRACK_MB: no se baja (protege la RAM de la replica)."""


def download_audio(audio_url: str) -> str:
    """Baja el original a un temporal, por partes y con tope de MAX_TRACK_MB."""
    tope = MAX_TRACK_MB * 1024 * 1024
    ext = os.path.splitext(audio_url.split("?")[0])[1] or ".audio"
    with requests.get(audio_url, timeout=120, stream=True) as r:
        r.raise_for_status()
        largo = int(r.headers.get("content-length") or 0)
        if largo > tope:
            raise ArchivoMuyGrande(f"archivo de {largo // (1024 * 1024)} MB (tope {MAX_TRACK_MB} MB)")
        tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
        n = 0
        try:
            for parte in r.iter_content(chunk_size=1024 * 1024):
                n += len(parte)
                if n > tope:
                    raise ArchivoMuyGrande(f"archivo de mas de {MAX_TRACK_MB} MB")
                tmp.write(parte)
        except BaseException:
            tmp.close()
            os.remove(tmp.name)
            raise
        tmp.close()
    return tmp.name


def send_result(job_id: str, track_id: str, status: str, result: dict = None, error: str = None):
    payload = {"job_id": job_id, "track_id": track_id, "status": status}
    if result is not None:
        payload["result"] = result
    if error:
        payload["error"] = sin_firma(error)[:1000]
    r = requests.post(f"{WORKER_API_URL}/worker-result", headers=HEADERS, json=payload, timeout=60)
    r.raise_for_status()


# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------
class Apagado(Exception):
    """Railway manda SIGTERM al redesplegar o al cambiar la configuración."""


APAGANDO = False      # tras SIGTERM: terminar lo que se pueda y no pedir más temas
EN_TRABAJO = False    # hay un trabajo reclamado en curso


def _al_apagar(_sig, _frame):
    """SIGTERM (3-oct-2026): un redespliegue cortaba las réplicas a mitad de un trabajo
    y el tema quedaba en `processing` hasta que el reclamo lo retomaba a los 8 min (6 temas
    del reanálisis de #320). Ahora el trabajo en curso se devuelve a la cola al instante
    (worker-result lo pasa a `pending`) y, sin trabajo, se sale limpio."""
    global APAGANDO
    APAGANDO = True
    if EN_TRABAJO:
        raise Apagado("reinicio del servicio (SIGTERM): el trabajo vuelve a la cola")
    print("SIGTERM sin trabajo en curso: salgo", flush=True)
    sys.exit(0)


def process_job(job: dict, track: dict, audio_url: str, rendition_upload: dict = None, master_upload: dict = None):
    job_id = job["id"]
    track_id = job["track_id"]
    print(f"[job {job_id}] track {track_id} — analizando...", flush=True)
    global EN_TRABAJO
    EN_TRABAJO = True
    tmp = None
    t0 = time.time()
    try:
        if not audio_url:
            send_result(job_id, track_id, "error", error="track sin audio")
            return
        # Tope por TRABAJO completo (no solo el analisis): por debajo del plazo de 8 min del
        # claim, asi ninguna otra replica lo retoma mientras este sigue (carga con 5 replicas).
        import signal
        def _tope(_s, _f):
            raise TimeoutError(f"trabajo mas largo que {TOPE_TRABAJO_S // 60} min (se corta para no trabar la cola)")
        signal.signal(signal.SIGALRM, _tope)
        signal.alarm(TOPE_TRABAJO_S)
        tmp = download_audio(audio_url)
        try:
            result = analyze(tmp, bpm_seed=track.get("bpm"))
        except AudioMudo as e:
            send_result(job_id, track_id, "done", result={"pista_vacia": True, "analysis_flags": ["silencio"]})
            print(f"[job {job_id}] {e}: marcada como pista vacía", flush=True)
            return
        # CM1-bis: loudness restaurado (la v5 lo habia perdido — regresion detectada 18-ago)
        # Genero (antes de convertir: las etiquetas viven en el original).
        try:
            gen = detectar_genero(tmp)
            result.update({k: v for k, v in gen.items() if k != "genero_por_revisar"})
            if gen.get("genero_por_revisar"):
                result["analysis_flags"] = list(result.get("analysis_flags") or []) + ["genero_por_revisar"]
            print(f"[job {job_id}] genero: {gen.get('genre_detected')} ({gen.get('genre_confidence')})", flush=True)
        except Exception as e:
            print(f"[job {job_id}] genero: fallo ({e})", flush=True)
        result.update(medir_sonoridad(tmp))
        if result.get("energy_v2") is not None:
            print(f"[job {job_id}] energia: {result.get('energy')} (v2: {result['energy_v2']})", flush=True)
        # CM2 (solo con ENABLE_ANCHOR_BACKFILL=true y examen aprobado): ancla de
        # precision sobre la RENDITION (lo que oye el DJ), nunca sobre el master.
        # Escribe SOLO first_beat_detected_ms; jamas first_beat_offset_ms ni _source.
        if cm2_habilitado():
            try:
                bpm_ref = track.get("bpm") or result.get("bpm")
                if bpm_ref and 40 < float(bpm_ref) < 240:
                    anc = ancla_de_rendicion(track_id, float(bpm_ref))
                    if anc["residuo_ms"] <= 8:
                        result["first_beat_detected_ms"] = int(round(anc["ancla_ms"]))
                        print(f"[job {job_id}] CM2 ancla={anc['ancla_ms']}ms residuo={anc['residuo_ms']}ms", flush=True)
                    else:
                        print(f"[job {job_id}] CM2 residuo alto ({anc['residuo_ms']}ms) — ancla NO escrita", flush=True)
            except Exception as e:
                print(f"[job {job_id}] CM2 fallo (no bloquea el job): {e}", flush=True)
        # Rendition: si la cancion no tiene version reproducible (ej. AIFF),
        # conviertela al ESTANDAR (MP3 CBR 192k) y subila via la URL firmada.
        # El backend debe fijar `stream_mp3_asset_path` con ese path; el campo
        # `stream_asset_path` (AAC) queda LEGACY y no se escribe mas.
        if rendition_upload and rendition_upload.get("url") and rendition_upload.get("path"):
            rend = make_rendition(tmp)
            if rend:
                if upload_rendition(rendition_upload["url"], rend):
                    result["rendition_path"] = rendition_upload["path"]
                    print(f"[job {job_id}] rendition subida: {rendition_upload['path']}", flush=True)
                else:
                    print(f"[job {job_id}] WARN: no se pudo subir la rendition", flush=True)
                try:
                    os.remove(rend)
                except Exception:
                    pass
        # Master MP3 320k (subidas masivas de WAV/AIFF/FLAC): worker-result cambia
        # audio_asset_path y borra el original solo despues de guardar la fila.
        if master_upload and master_upload.get("url") and master_upload.get("path") and track.get("needs_master_conversion"):
            master = make_rendition(tmp, bitrate=MASTER_BITRATE, sufijo=".master", etiquetas=True)
            if master:
                if upload_rendition(master_upload["url"], master):
                    result["master_path"] = master_upload["path"]
                    print(f"[job {job_id}] master MP3 320k subido: {master_upload['path']}", flush=True)
                else:
                    print(f"[job {job_id}] WARN: no se pudo subir el master MP3", flush=True)
                try:
                    os.remove(master)
                except Exception:
                    pass
        # respetar bpm/key de tags: el backend solo los usa si el track no los tenía
        result["bpm"] = result.get("bpm")  # enviar siempre el BPM preciso (con decimales)
        result["key"] = result.get("key") if not track.get("key") else None
        # Fase 2 — identificar por huella SOLO si el track no trae artista/título.
        # El backend (worker-result) escribe estos campos únicamente si están vacíos.
        if not (track.get("artist") and track.get("title")):
            ident = fingerprint_identify(tmp)
            if ident:
                result["identified_artist"] = ident["artist"]
                result["identified_title"] = ident["title"]
                print(f"[job {job_id}] identificado: {ident['artist']} — {ident['title']}", flush=True)
        send_result(job_id, track_id, "done", result=result)
        n_cues = len(result.get("cue_points") or [])
        # El tiempo total sirve para ajustar TOPE_TRABAJO_S con datos de Railway (carga masiva).
        print(f"[job {job_id}] OK en {time.time() - t0:.0f} s (tema de {result.get('duration_seconds') or '?'} s) "
              f"— cues={n_cues} energy={result['energy']}", flush=True)
    except Exception as e:
        import signal
        signal.alarm(0)  # que el tope no corte el aviso de error
        traceback.print_exc()
        # «determinista:» = reintentar da lo mismo (como en grid_verifier). worker-result
        # hoy reintenta todo error hasta 3 veces; con este prefijo puede cerrarlo de una.
        error = f"determinista:{e}" if isinstance(e, ArchivoMuyGrande) else str(e)
        try:
            send_result(job_id, track_id, "error", error=error)
        except Exception:
            pass
        print(f"[job {job_id}] FALLO en {time.time() - t0:.0f} s: {error}", flush=True)
    finally:
        import signal
        signal.alarm(0)
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
        EN_TRABAJO = False


# ============================================================================
# v7 — RENDERIZADOR DE SETS PRE-MEZCLADOS (offline)
# ============================================================================
# Un set tiene FINAL, a diferencia de la radio 24/7. Por eso NO necesita
# Icecast ni VPS de streaming: se mezcla UNA vez offline y queda como archivo.
# Ventaja: calidad de mezcla sin apuro, y reproduce perfecto en segundo plano
# y con el telefono bloqueado (que es justo el techo del modelo client-side).
#
# Metodologia aplicada (skills del proyecto + investigacion):
#   * Transiciones sobre los CUE POINTS reales (MIX-OUT saliente / MIX-IN entrante)
#   * Crossfade EQUAL-POWER: 0.707 en el medio, NO 0.5 -> sin hueco de volumen
#   * BASS-SWAP: el entrante entra sin graves, el saliente los cede -> sin dos
#     kicks peleando (cancelacion de fase = "barro")
#   * Solape alineado a limite de compas
#   * Time-stretch solo si dBPM <= 6%; mas alla NO se fuerza
#   * Normalizacion a -14 LUFS con techo de true peak
# ----------------------------------------------------------------------------

def _set_api(action, payload=None):
    url = f"{WORKER_API_URL}/set-render?action={action}"
    r = requests.post(url, headers={"x-worker-secret": WORKER_SECRET,
                                    "Content-Type": "application/json"},
                      json=payload or {}, timeout=120)
    r.raise_for_status()
    return r.json()


def _decode_pcm(path, sr=SET_SR):
    """Decodifica a float32 estereo -> array (n, 2)."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-f", "f32le", "-acodec", "pcm_f32le",
         "-ar", str(sr), "-ac", "2", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.float32).reshape(-1, 2).copy()


def _stretch(path, ratio, tmpdir):
    """Time-stretch preservando el tono. Cae a atempo si no hay rubberband."""
    if abs(ratio - 1.0) < 1e-4:
        return path
    dst = os.path.join(tmpdir, f"st_{abs(hash((path, ratio)))}.wav")
    for filtro in (f"rubberband=tempo={ratio:.6f}", f"atempo={ratio:.6f}"):
        try:
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-af", filtro,
                            "-ar", str(SET_SR), "-ac", "2", dst],
                           capture_output=True, check=True)
            return dst
        except subprocess.CalledProcessError:
            continue
    return path


def _low_shelf(x, fc, gain_db):
    """Low-shelf biquad (RBJ). gain_db<0 recorta graves."""
    from scipy.signal import lfilter
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * np.pi * fc / SET_SR
    alpha = np.sin(w0) / 2 * np.sqrt((A + 1 / A) * (1 / 0.707 - 1) + 2)
    cw, sq = np.cos(w0), 2 * np.sqrt(A) * alpha
    b = np.array([A * ((A + 1) - (A - 1) * cw + sq),
                  2 * A * ((A - 1) - (A + 1) * cw),
                  A * ((A + 1) - (A - 1) * cw - sq)])
    a0 = (A + 1) + (A - 1) * cw + sq
    a = np.array([1.0, (-2 * ((A - 1) + (A + 1) * cw)) / a0,
                  ((A + 1) + (A - 1) * cw - sq) / a0])
    b = b / a0
    y = np.empty_like(x)
    for ch in range(x.shape[1]):
        y[:, ch] = lfilter(b, a, x[:, ch])
    return y


def _bass_ramp(x, entrando: bool):
    """Bass-swap progresivo entre version filtrada y plena."""
    filt = _low_shelf(x, BASS_HZ, -24.0)
    t = np.linspace(0.0 if entrando else 1.0, 1.0 if entrando else 0.0, len(x))[:, None]
    return (filt * (1 - t) + x * t).astype(np.float32)


def _cue(cues, label, default=None):
    for c in cues or []:
        if (c.get("label") or "").upper() == label:
            return float(c["positionMs"])
    return default


def _bajar_tema(url, dest):
    """Baja el audio de un tema del set. Si falla, el error dice solo el codigo y el
    host: la URL firmada (con su token) no llega al log ni a set_render_jobs.error."""
    from urllib.parse import urlsplit
    host = urlsplit(url or "").hostname or "?"
    try:
        r = requests.get(url, timeout=300)
    except requests.RequestException as e:
        raise RuntimeError(f"descarga fallida ({type(e).__name__}) desde {host}") from None
    if r.status_code >= 400:
        raise RuntimeError(f"descarga fallida (HTTP {r.status_code}) desde {host}")
    with open(dest, "wb") as f:
        f.write(r.content)


def _mezcla_libre(tracks, tmpdir):
    """Metodo anterior (sin plan): tempo unico del set, cruces de XFADE_BARS sobre los cues
    MIX-IN/MIX-OUT. Solo para trabajos sin `spec.transiciones` (sets viejos)."""
    salida = np.zeros((0, 2), dtype=np.float32)
    tracklist, bpm_set, fin_ant, fase_ant = [], None, 0, 0.0

    for i, tr in enumerate(tracks):
        titulo = tr.get("title") or "?"
        print(f"  [{i+1}/{len(tracks)}] {titulo}", flush=True)
        p = os.path.join(tmpdir, f"{i}.audio")
        _bajar_tema(tr.get("audio_url"), p)

        bpm_tr = float(tr.get("bpm") or 0) + float(tr.get("bpm_fine") or 0)
        if not bpm_tr:
            print("    sin BPM, se omite", flush=True)
            continue

        if bpm_set is None:
            bpm_set, ratio = bpm_tr, 1.0
        else:
            ratio = bpm_set / bpm_tr
            desvio = abs(ratio - 1.0) * 100
            if desvio > MAX_STRETCH_PCT:
                print(f"    dBPM {desvio:.1f}% > {MAX_STRETCH_PCT}% — sin estirar", flush=True)
                ratio = 1.0
            elif desvio > 0.05:
                p = _stretch(p, 1.0 / ratio, tmpdir)

        audio = _decode_pcm(p)
        factor = ratio if ratio != 1.0 else 1.0
        cues = tr.get("cue_points") or []
        dur_ms = len(audio) / SET_SR * 1000.0
        anchor_ms = float(tr.get("first_beat_offset_ms") or 0) * factor
        mix_in = (_cue(cues, "MIX-IN", 0.0) or 0.0) * factor
        mix_out = _cue(cues, "MIX-OUT")
        mix_out = dur_ms * 0.90 if mix_out is None else mix_out * factor

        beat_ms = 60000.0 / bpm_set
        compas_ms = beat_ms * 4
        audio = audio[int(mix_in / 1000.0 * SET_SR):]
        fase = (mix_in - anchor_ms) % compas_ms

        if len(salida) == 0:
            salida = audio
            tracklist.append({"position": 1, "start_seconds": 0, **_tl(tr)})
            fin_ant = int((mix_out - mix_in) / 1000.0 * SET_SR)
            fase_ant = fase
            continue

        disp_ms = min(mix_out - mix_in, fin_ant / SET_SR * 1000.0)
        bars = XFADE_BARS
        while bars > MIN_XFADE_BARS and bars * compas_ms > disp_ms * 0.5:
            bars -= 4
        n = int(min(bars * compas_ms, max(disp_ms, 0) * 0.5) / 1000.0 * SET_SR)
        n = max(1, min(n, len(audio), len(salida)))

        offset = int((((fase - fase_ant) % compas_ms) / 1000.0) * SET_SR)
        ini = max(0, min(fin_ant - n + offset, len(salida) - n))

        t = np.linspace(0, np.pi / 2, n)[:, None]
        fo, fi = np.cos(t).astype(np.float32), np.sin(t).astype(np.float32)
        cola = _bass_ramp(salida[ini:ini + n], entrando=False)
        cabeza = _bass_ramp(audio[:n], entrando=True)
        mezcla = cola * fo + cabeza * fi
        salida = np.vstack([salida[:ini], mezcla, audio[n:]])

        tracklist.append({"position": i + 1, "start_seconds": int(ini / SET_SR), **_tl(tr)})
        print(f"    transicion {bars} compases en {ini/SET_SR/60:.1f} min", flush=True)
        fin_ant = len(salida) - max(0, len(audio) - int((mix_out - mix_in) / 1000.0 * SET_SR))
        fase_ant = fase
    return salida, tracklist


def _masterizar_y_subir(salida, tmpdir, upload_url, result_path):
    """Loudness parejo + techo de true peak, MP3 256k y subida. Devuelve la duracion en s."""
    try:
        import pyloudnorm as pyln
        lufs = pyln.Meter(SET_SR).integrated_loudness(salida.mean(axis=1))
        if np.isfinite(lufs):
            salida = salida * (10 ** ((SET_TARGET_LUFS - lufs) / 20.0))
            print(f"  loudness {lufs:.1f} -> {SET_TARGET_LUFS} LUFS", flush=True)
    except Exception:
        pass
    pico = float(np.max(np.abs(salida))) or 1.0
    techo = 10 ** (-1.0 / 20.0)
    if pico > techo:
        salida = salida * (techo / pico)

    wav = os.path.join(tmpdir, "set.wav")
    import wave
    with wave.open(wav, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(SET_SR)
        w.writeframes((np.clip(salida, -1, 1) * 32767).astype(np.int16).tobytes())
    mp3 = os.path.join(tmpdir, "set.mp3")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", wav,
                    "-c:a", "libmp3lame", "-b:a", "256k", mp3], check=True)

    with open(mp3, "rb") as f:
        up = requests.put(upload_url, data=f, headers={"Content-Type": "audio/mpeg"}, timeout=900)
    up.raise_for_status()
    dur = len(salida) / SET_SR
    print(f"  subido: {result_path} — {dur/60:.1f} min", flush=True)
    return dur




# ----------------------------------------------------------------------------
# #143 · El set sigue el PLAN revisado por el DJ (spec.transiciones)
# ----------------------------------------------------------------------------
# «Convertir en set» arma cada transicion con el mismo planificador que suena en
# las listas (planTransition del navegador) y la manda en spec.transiciones. Aqui
# se reproduce lo mismo offline, con las mismas cifras que el navegador:
#   * cada tema suena a su tempo propio; la entrante va a `rate` durante la mezcla
#     y vuelve a 1 en `release_seg` (rampa lineal: se renderiza con el promedio);
#   * ganancias equal-power con el punto medio en `asimetria` (mixGainsAt);
#   * graves: shelf en 120 Hz. Con swap: entrante -12 dB hasta `graves_swap_en`
#     y la saliente cae a -12 dB desde ahi (rampa de 1/4 de ventana). Sin swap:
#     entrante -6 dB -> 0 dB;
#   * eco: 1 beat de retardo, realimentacion 0,45, la seca cae en medio beat y la
#     cola dura 8 beats (echoOutSettings); la entrante cae en el downbeat;
#   * corte y encadenado: la entrante arranca justo despues.
SHELF_HZ = 120.0
BASS_IN_START_DB, BASS_SWAP_IN_DB, BASS_OUT_END_DB = -6.0, -12.0, -12.0
ECO_FEEDBACK, ECO_WET = 0.45, 0.7
TIPOS_TRANSICION = ("mezcla", "eco", "corte", "encadenado")


def _id_de(tr):
    return tr.get("id") or tr.get("track_id")


def plan_valido(tracks, transiciones):
    """True si hay una transicion por par seguido, en el mismo orden que los temas."""
    if not isinstance(transiciones, list) or len(tracks) < 2 or len(transiciones) != len(tracks) - 1:
        return False
    for k, t in enumerate(transiciones):
        if not isinstance(t, dict) or t.get("tipo") not in TIPOS_TRANSICION:
            return False
        if t.get("desde") != _id_de(tracks[k]) or t.get("hasta") != _id_de(tracks[k + 1]):
            return False
    return True


def _num(v, defecto=None):
    try:
        x = float(v)
        return x if math.isfinite(x) else defecto
    except (TypeError, ValueError):
        return defecto


def ganancias_mezcla(t, asimetria=0.6):
    """Equal-power con el punto medio corrido (mixGainsAt). t en 0..1 (array)."""
    a = min(0.95, max(0.05, asimetria))
    t = np.clip(t, 0.0, 1.0)
    p = np.where(t <= a, 0.5 * t / a, 0.5 + 0.5 * (t - a) / (1 - a))
    return np.cos(p * np.pi / 2), np.sin(p * np.pi / 2)


def _rampa_despues(t, en):
    a = min(0.95, max(0.05, en))
    largo = min(0.25, 1 - a) or 1e-6
    return np.where(t <= a, 0.0, np.minimum(1.0, (t - a) / largo))


def curvas_graves(t, swap, swap_en):
    """dB del shelf de graves (entrante, saliente) en cada t (buildCurves)."""
    if swap:
        return BASS_SWAP_IN_DB * (1 - _rampa_despues(t, swap_en)), BASS_OUT_END_DB * _rampa_despues(t, swap_en)
    return BASS_IN_START_DB * (1 - t), np.zeros_like(t)


def _graves(x, db):
    """Aplica `db` (array por muestra) a la banda bajo SHELF_HZ (graves + resto = senal)."""
    if not np.any(db):
        return x
    from scipy.signal import butter, sosfiltfilt
    sos = butter(2, SHELF_HZ, btype="low", fs=SET_SR, output="sos")
    bajo = sosfiltfilt(sos, x, axis=0).astype(np.float32)
    g = (10.0 ** (db / 20.0)).astype(np.float32)[:, None]
    return (x - bajo) + bajo * g


def _estirar_pcm(x, tempo, tmpdir):
    """Time-stretch de un bloque PCM (tempo > 1 acorta) preservando el tono."""
    if abs(tempo - 1.0) < 1e-4 or len(x) == 0:
        return x
    src = os.path.join(tmpdir, f"bloque_{abs(hash((len(x), tempo)))}.f32")
    x.astype(np.float32).tofile(src)
    for filtro in (f"rubberband=tempo={tempo:.6f}", f"atempo={tempo:.6f}"):
        try:
            out = subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "f32le", "-ar", str(SET_SR), "-ac", "2", "-i", src,
                 "-af", filtro, "-f", "f32le", "-ar", str(SET_SR), "-ac", "2", "-"],
                capture_output=True, check=True).stdout
            return np.frombuffer(out, dtype=np.float32).reshape(-1, 2).copy()
        except subprocess.CalledProcessError:
            continue
    print(f"    ⚠ no se pudo estirar a tempo {tempo:.4f} (ni rubberband ni atempo)", flush=True)
    return None


class SinEstirar(RuntimeError):
    """La entrante no se pudo igualar al tempo de la saliente."""


def _unir(a, b, n=int(0.01 * SET_SR)):
    """Pega dos bloques con un cruce de 10 ms (sin clic entre tramos estirados)."""
    n = min(n, len(a), len(b))
    if n <= 0:
        return np.vstack([a, b])
    t = np.linspace(0.0, 1.0, n, dtype=np.float32)[:, None]
    return np.vstack([a[:-n], a[-n:] * (1 - t) + b[:n] * t, b[n:]])


def linea_entrante(audio, entrada_seg, dur_seg, rate, release_seg, tmpdir):
    """Audio de la entrante tal como suena desde que entra: `dur_seg` a `rate`, la
    rampa de vuelta a 1 en `release_seg` y el resto a su tempo. Devuelve (pcm, tramos)
    con tramos = [(seg_del_tema, seg_de_la_linea, rate)] para ubicar puntos del tema."""
    sr = SET_SR
    pos = int(max(0.0, entrada_seg) * sr)
    if not rate or abs(rate - 1.0) < 1e-4 or dur_seg <= 0:
        return audio[pos:], [(entrada_seg, 0.0, 1.0)]
    rel = max(0.0, release_seg or 0.0)
    medio = (1.0 + rate) / 2.0
    n1 = int(dur_seg * rate * sr)
    n2 = int(rel * medio * sr)
    tramo1 = _estirar_pcm(audio[pos:pos + n1], rate, tmpdir)
    tramo2 = _estirar_pcm(audio[pos + n1:pos + n1 + n2], medio, tmpdir)
    if tramo1 is None or tramo2 is None:
        raise SinEstirar(f"rate {rate:.4f}")
    resto = audio[pos + n1 + n2:]
    pcm = _unir(_unir(tramo1, tramo2), resto)
    tramos = [(entrada_seg, 0.0, rate),
              (entrada_seg + dur_seg * rate, dur_seg, medio),
              (entrada_seg + dur_seg * rate + rel * medio, dur_seg + rel, 1.0)]
    return pcm, tramos


def seg_en_linea(tramos, seg_tema):
    """Segundo de la linea (salida) en que suena `seg_tema` del tema."""
    base_t, base_l, r = tramos[0]
    for t0, l0, rr in tramos:
        if seg_tema >= t0:
            base_t, base_l, r = t0, l0, rr
    return base_l + (seg_tema - base_t) / r


def fin_util(audio, umbral_db=-60.0):
    """Ultima muestra con senal (sin la cola de silencio)."""
    env = np.max(np.abs(audio), axis=1) if len(audio) else np.zeros(0)
    vivas = np.nonzero(env > 10 ** (umbral_db / 20.0))[0]
    return int(vivas[-1]) + 1 if len(vivas) else len(audio)


def cola_eco(seca, bpm):
    """Cola del eco a tempo (echoOut.ts): repeticiones cada beat con realimentacion 0,45
    de lo que la saliente toca en el medio beat del envio; la cola dura 8 beats."""
    beat = 60.0 / bpm if bpm and bpm > 0 else 0.5
    d = int(beat * SET_SR)
    envio = seca  # lo que entra al eco: la saliente desde el downbeat, medio beat
    cola = np.zeros((int(beat * 8 * SET_SR) + len(envio), 2), dtype=np.float32)
    g = 1.0
    for k in range(1, 64):
        ini = k * d
        if ini >= len(cola) or g < 1e-3:
            break
        fin = min(len(cola), ini + len(envio))
        cola[ini:fin] += envio[:fin - ini] * g
        g *= ECO_FEEDBACK
    cola *= ECO_WET
    n_off = int(len(cola) * 0.25)  # ultimo cuarto: el retorno baja a 0
    if n_off:
        cola[-n_off:] *= np.linspace(1.0, 0.0, n_off, dtype=np.float32)[:, None]
    return cola


def _mezcla_plan(tracks, transiciones, tmpdir):
    """El set con el plan de cada transicion. Devuelve (salida, tracklist)."""
    sr = SET_SR
    salida = np.zeros((0, 2), dtype=np.float32)
    tracklist = []
    ini_linea, tramos_linea = 0, [(0.0, 0.0, 1.0)]  # donde arranca la linea del tema actual
    for i, tr in enumerate(tracks):
        print(f"  [{i+1}/{len(tracks)}] {tr.get('title') or '?'}", flush=True)
        p = os.path.join(tmpdir, f"{i}.audio")
        _bajar_tema(tr.get("audio_url"), p)
        audio = _decode_pcm(p)
        if i == 0:
            salida = audio
            tracklist.append({"position": 1, "start_seconds": 0, **_tl(tr)})
            continue

        t = transiciones[i - 1]
        tipo = t["tipo"]
        ant = tracks[i - 1]
        bpm_ant = _num(ant.get("bpm"), 0) + _num(ant.get("bpm_fine"), 0)
        salida_seg = _num(t.get("salida_seg"))
        if salida_seg is None:  # al final util de la saliente
            corte = fin_util(salida[ini_linea:]) + ini_linea
        else:
            corte = ini_linea + int(seg_en_linea(tramos_linea, salida_seg) * sr)
        corte = max(ini_linea, min(corte, len(salida)))
        entrada = _num(t.get("entrada_seg"), 0.0)

        if tipo == "mezcla":
            dur = max(0.0, _num(t.get("duracion_seg"), 0.0))
            try:
                linea, tramos = linea_entrante(audio, entrada, dur, _num(t.get("rate")),
                                               _num(t.get("release_seg"), 0.0), tmpdir)
            except SinEstirar as e:
                # Como el planificador: dos tempos distintos nunca se cruzan sin igualar.
                # Sin estirar, la transicion pasa con eco en el mismo downbeat.
                print(f"    ⚠ mezcla sin igualar tempo ({e}): pasa con eco", flush=True)
                tipo = "eco"
        if tipo == "mezcla":
            n = max(1, min(int(dur * sr), len(linea), len(salida) - corte))
            x = np.linspace(0.0, 1.0, n)
            g_out, g_in = ganancias_mezcla(x, _num(t.get("asimetria"), 0.6))
            db_in, db_out = curvas_graves(x, bool(t.get("graves_swap")), _num(t.get("graves_swap_en"), 0.5))
            sale = _graves(salida[corte:corte + n], db_out) * g_out[:, None].astype(np.float32)
            entra = _graves(linea[:n], db_in) * g_in[:, None].astype(np.float32)
            salida = np.vstack([salida[:corte], sale + entra, linea[n:]])
            print(f"    mezcla {t.get('compases') or 0} compases ({dur:.1f} s, rate {_num(t.get('rate'), 1.0):.4f}) "
                  f"en {corte/sr/60:.1f} min", flush=True)
        else:
            pos = int(max(0.0, entrada) * sr)
            linea, tramos = audio[pos:], [(entrada, 0.0, 1.0)]
            if tipo == "eco":
                n_seca = min(int(60.0 / (bpm_ant or 120.0) / 2 * sr), len(salida) - corte)
                seca = salida[corte:corte + n_seca] * np.linspace(1.0, 0.0, n_seca, dtype=np.float32)[:, None]
                cola = cola_eco(salida[corte:corte + n_seca], bpm_ant)
                base = linea.copy()
                base[:len(seca)] += seca[:len(base)]
                m = min(len(cola), len(base))
                base[:m] += cola[:m]
                salida = np.vstack([salida[:corte], base])
            else:  # corte o encadenado: 30 ms de salida para no hacer clic
                n_f = min(int(0.03 * sr), corte - ini_linea)
                if n_f > 0:
                    salida[corte - n_f:corte] *= np.linspace(1.0, 0.0, n_f, dtype=np.float32)[:, None]
                salida = np.vstack([salida[:corte], linea])
            print(f"    {tipo} en {corte/sr/60:.1f} min ({t.get('razon') or 'sin razon'})", flush=True)

        tracklist.append({"position": i + 1, "start_seconds": round(corte / sr, 3), **_tl(tr)})
        ini_linea, tramos_linea = corte, tramos
    return salida, tracklist


def render_set(job, tracks, upload_url, result_path):
    """Mezcla el set completo y lo sube. Devuelve (duracion_s, tracklist).
    Con `spec.transiciones` valido sigue el plan revisado por el DJ (#143); si no,
    el metodo anterior."""
    tmpdir = tempfile.mkdtemp(prefix="dm_set_")
    try:
        transiciones = ((job or {}).get("spec") or {}).get("transiciones")
        if plan_valido(tracks, transiciones):
            print(f"  plan del DJ: {len(transiciones)} transiciones (" +
                  ", ".join(t["tipo"] for t in transiciones) + ")", flush=True)
            salida, tracklist = _mezcla_plan(tracks, transiciones, tmpdir)
        else:
            if transiciones:
                print("  spec.transiciones no coincide con los temas: metodo anterior", flush=True)
            salida, tracklist = _mezcla_libre(tracks, tmpdir)
        return _masterizar_y_subir(salida, tmpdir, upload_url, result_path), tracklist
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _tl(tr):
    return {"track_id": tr.get("id") or tr.get("track_id"),
            "title": tr.get("title"), "artist": tr.get("artist"),
            "label": tr.get("label")}


# ----------------------------------------------------------------------------
# #265 · Limpieza de las copias de escucha ya subidas (en tandas, por la cola)
# ----------------------------------------------------------------------------
# Contrato con la plataforma (funcion `stream-limpiar`, x-worker-secret):
#   POST ?action=next   -> {job: null} | {job: {id, track_id}, audio_url, upload: {url, path}}
#                          audio_url = la copia de escucha actual (firmada);
#                          upload    = la MISMA ruta, con upsert: la base no cambia.
#   POST ?action=result -> {job_id, path, bytes}
#   POST ?action=fail   -> {job_id, error}
# Sin re-codificar: se copia el audio tal cual y solo se quitan las etiquetas, asi
# que no se pierde calidad ni cambia el timeline (rejilla y cues siguen validos).
# Si la funcion no existe todavia (404), el worker la ignora.
LIMPIEZA_DISPONIBLE = True


def tiene_etiquetas(path: str) -> bool:
    """True si el archivo trae ID3v2 al principio, ID3v1 al final o atomos de texto MP4."""
    with open(path, "rb") as f:
        cab = f.read(10)
        f.seek(0, os.SEEK_END)
        n = f.tell()
        f.seek(max(0, n - 128))
        cola = f.read(128)
        f.seek(0)
        todo = f.read() if n < 64 * 1024 * 1024 else b""
    if cab[:3] == b"ID3" or cola[:3] == b"TAG":
        return True
    return any(a in todo for a in (b"\xa9nam", b"\xa9ART", b"covr"))


def limpiar_etiquetas(src: str, ext: str) -> str:
    """Copia el audio sin etiquetas ni portada. Devuelve la ruta nueva (o lanza)."""
    out = src + ".limpio" + ext
    fmt = ["-f", "mp3"] if ext == ".mp3" else ["-f", "ipod", "-movflags", "+faststart"]
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", src, "-map", "0:a:0", "-c:a", "copy",
         *SIN_ETIQUETAS, *(["-write_xing", "1"] if ext == ".mp3" else []), *fmt, out],
        capture_output=True, timeout=180)
    if proc.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
        raise RuntimeError(f"ffmpeg no pudo limpiar ({proc.returncode})")
    if tiene_etiquetas(out):
        raise RuntimeError("la copia limpia todavia tiene etiquetas")
    return out


def _limpiar_api(action, payload=None):
    r = requests.post(f"{WORKER_API_URL}/stream-limpiar?action={action}", headers=HEADERS,
                      json=payload or {}, timeout=60)
    if r.status_code == 404 and action == "next":
        return None
    r.raise_for_status()
    return r.json()


def poll_limpiar_streams() -> bool:
    """Toma una copia de escucha de la cola, le quita las etiquetas y la vuelve a subir
    en la misma ruta. True si hizo algo. No escribe en la base: la funcion guarda el resultado."""
    global LIMPIEZA_DISPONIBLE
    if not LIMPIEZA_DISPONIBLE:
        return False
    try:
        data = _limpiar_api("next")
    except Exception as e:
        print(f"[limpiar-stream] no disponible: {type(e).__name__}", flush=True)
        return False
    if data is None:
        LIMPIEZA_DISPONIBLE = False  # la funcion no existe: no se vuelve a preguntar hasta reiniciar
        print("[limpiar-stream] la funcion stream-limpiar no existe todavia: se omite", flush=True)
        return False
    job = data.get("job")
    if not job:
        return False
    up = data.get("upload") or {}
    path = up.get("path") or ""
    ext = ".m4a" if path.lower().endswith((".m4a", ".mp4", ".aac")) else ".mp3"
    tmp = os.path.join(tempfile.mkdtemp(prefix="dm_limpiar_"), "in" + ext)
    try:
        r = requests.get(data.get("audio_url"), timeout=120)
        if r.status_code >= 400:
            raise RuntimeError(f"descarga fallida (HTTP {r.status_code})")
        with open(tmp, "wb") as f:
            f.write(r.content)
        if tiene_etiquetas(tmp):
            limpio = limpiar_etiquetas(tmp, ext)
            with open(limpio, "rb") as f:
                put = requests.put(up["url"], data=f, timeout=300,
                                   headers={"Content-Type": "audio/mpeg" if ext == ".mp3" else "audio/mp4",
                                            "x-upsert": "true"})
            if put.status_code >= 400:
                raise RuntimeError(f"subida fallida (HTTP {put.status_code})")
            n = os.path.getsize(limpio)
            print(f"[limpiar-stream] {job['track_id']}: sin etiquetas ({n} bytes)", flush=True)
        else:
            n = os.path.getsize(tmp)
            print(f"[limpiar-stream] {job['track_id']}: ya estaba limpia", flush=True)
        _limpiar_api("result", {"job_id": job["id"], "path": path, "bytes": n})
    except Exception as e:
        print(f"[limpiar-stream] {job.get('track_id')}: fallo ({sin_firma(e)[:200]})", flush=True)
        try:
            _limpiar_api("fail", {"job_id": job["id"], "error": sin_firma(e)[:500]})
        except Exception:
            pass
    finally:
        shutil.rmtree(os.path.dirname(tmp), ignore_errors=True)
    return True


def _sin_firmas(texto):
    """Quita el query string (token de las URLs firmadas) de un mensaje de error."""
    import re
    return re.sub(r"(https?://[^\s?'\"]+)\?[^\s'\"]*", r"\1?…", texto)


def poll_set_render():
    """Busca un job de render de set y lo procesa. Devuelve True si hizo algo."""
    try:
        data = _set_api("next")
    except Exception as e:
        print(f"[set-render] no disponible: {e}", flush=True)
        return False
    job = data.get("job")
    if not job:
        return False
    print(f"[set-render] job {job['id']} — {job.get('title')}", flush=True)
    try:
        dur, tracklist = render_set(job, data.get("tracks") or [],
                                    data.get("upload_url"), data.get("result_path"))
        _set_api("result", {"job_id": job["id"], "result_path": data.get("result_path"),
                            "duration_sec": int(dur), "tracklist": tracklist})
        print(f"[set-render] OK — set creado SIN publicar (revisar antes de publicar)", flush=True)
    except Exception as e:
        traceback.print_exc()
        try:
            # _sin_firmas solo ve URLs con https://; requests dice «with url: /ruta?token=…».
            _set_api("fail", {"job_id": job["id"], "error": sin_firma(_sin_firmas(str(e)))[:2000]})
        except Exception:
            pass
    return True


def liberar_memoria():
    """Devuelve al sistema la RAM de los picos de un analisis (v7.5.1).
    numpy/librosa piden varios GB por tema (WAV de 65 MB, filtros en float64) y
    glibc no los devuelve: cada replica quedaba con su maximo (~4 GB ociosos,
    11,9 GB de promedio en 3 replicas = ~US$120/mes solo en RAM)."""
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass  # fuera de Linux/glibc no hay malloc_trim


def main():
    filtrar_salida()
    print("DeepMancho worker iniciado (v7.6.10: SIGTERM devuelve el trabajo a la cola; loudness_lufs en estereo BS.1770 (#320); examen CM2 con golden set sintetico (#573); tempo mas rapido con el mismo resultado; tiempo por trabajo en el log y archivo muy grande como falla determinista; tempo sin BPM previo tambien con semilla de 2/3; genero detectado por etiqueta; carga masiva con tope por trabajo y MAX_TRACK_MB; el set sigue el plan del DJ; tempo correcto sin BPM previo; CM2 con x-worker-secret y solo con examen aprobado; HOT CUES metodologia MIK sobre el ancla DEFINITIVA + plan B por rejilla de frases y plan C por tiempo: ningun tema queda sin cues). Esperando jobs...", flush=True)
    if ENABLE_SET_RENDER:
        print("[set-render] habilitado — se atenderan jobs de render de sets", flush=True)
    global EXAMEN_CM2_APROBADO
    if GOLDEN_EXAM:
        try:
            EXAMEN_CM2_APROBADO = bool(golden_exam())
        except Exception:
            traceback.print_exc()
            print("[CM2 EXAMEN] el examen fallo pero el worker sigue normal", flush=True)
        liberar_memoria()
    import signal
    signal.signal(signal.SIGTERM, _al_apagar)
    idle = 0
    espera = Espera(POLL_INTERVAL, POLL_MAX)
    while True:
        if APAGANDO:
            print("apagando: no pido más temas", flush=True)
            sys.exit(0)
        try:
            job, track, audio_url, subidas = next_job()
        except Exception as e:
            print(f"next_job error: {e}", flush=True)
            time.sleep(espera.error())
            continue
        if job:
            idle = 0
            espera.trabajo()
            process_job(job, track, audio_url, (subidas or {}).get("rendition"), (subidas or {}).get("master"))
            liberar_memoria()
        else:
            # Sin jobs de analisis: aprovechar para renderizar sets si hay cola.
            # El analisis tiene prioridad (un track sin analizar bloquea mas que
            # un set sin renderizar).
            if ENABLE_SET_RENDER and poll_set_render():
                idle = 0
                espera.trabajo()
                liberar_memoria()
                continue
            # #265: con la cola vacia, limpiar etiquetas de copias de escucha viejas.
            if poll_limpiar_streams():
                idle = 0
                espera.trabajo()
                continue
            idle += 1
            if idle % 12 == 1:
                print(f"sin jobs pendientes... (proxima consulta en {espera.actual:.0f} s)", flush=True)
            time.sleep(espera.vacia())


if __name__ == "__main__":
    main()
