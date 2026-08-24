"""
DeepMancho — Worker de análisis de audio (server-side).

Procesa una cola `analysis_jobs` en Supabase:
  1. Reclama un job pendiente (RPC atómico claim_analysis_job).
  2. Descarga el audio del track desde Storage (rendition m4a o master).
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
import sys
import time
import math
import json
import tempfile
import traceback
import subprocess

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
# Fase 2 (opcional): identificación por huella acústica (Chromaprint + AcoustID).
# Si no está la key o falta `fpcalc`, el worker sigue funcionando igual sin identificar.
ACOUSTID_API_KEY = os.environ.get("ACOUSTID_API_KEY", "")

SR = 11025          # más liviano que 22050; suficiente para beat/estructura/energía
MAX_DURATION = 600  # analiza como máximo 10 min (tope de tiempo/memoria)
# Resolución de la waveform. 800 se veía en bloques al hacer zoom en el mezclador;
# 3000 da ~4x de detalle para el zoom por compás sin inflar demasiado el payload.
BUCKETS = 3000
HEADERS = {"x-worker-secret": WORKER_SECRET, "Content-Type": "application/json"}

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
ENABLE_MIX_V7 = os.environ.get("ENABLE_MIX_V7", "0") == "1"  # v7.1 MIX-IN/OUT refutados: apagados por default
ANCHOR_SR = 22050    # SR del análisis de ancla (independiente del SR=11025 general)
ANCHOR_HOP = 128     # ~5.8 ms por frame de onset a 22050 Hz
ANCHOR_TOL_MS = 10.0 # criterio del examen (error relativo por par)

# Golden set — anclas validadas por oído + telemetría (18-ago-2026).
# gold_ms = first_beat_detected_ms vigente en la base tras la calibración manual.
GOLDEN_TRACKS = [
    # (track_id, titulo, bpm, gold_ancla_ms_MOD_BEAT)
    # RECALIBRADO 23-ago-2026 con la metodologia certificada de
    # docs/golden-set-mixer.md (banda de kick 35-130 Hz Butterworth + envolvente
    # de Hilbert + ataque al 25% entre piso y pico, 90 s desde el 35% del track),
    # medido de forma INDEPENDIENTE del worker. Los gold anteriores (81/128/238/
    # 158/398/372) venian de la metodologia vieja basada en el PICO y resultaron
    # dispersos (-140 a +170 ms), no un corrimiento constante: eran la regla
    # equivocada. Validacion cruzada: el detector de la v7.2 coincidio con la
    # medicion independiente en 5 de 6 tracks dentro de +-5 ms.
    ("b411743d-de03-4190-b6fe-f44aa6685ba8", "Make It Hot (Mustafa Ismaeel Rmx)", 122.0, 12),
    ("a16963a1-0d15-4354-80e5-ba27500dd7b1", "Blame (Claptone Extended Mix)",     122.0, 57),
    ("4cc427fb-03a6-4165-8ca3-2025b6ebe779", "No Time for Tears (Original Mix)",  122.0, 98),
    ("7d377de8-6562-416d-8b0b-97317f9b6c7f", "Slip Away (Original Mix)",          122.0, 41),
    ("cfaaaa0e-26d1-4e96-ab3c-8a5a49f34f07", "Right Thing (Instrumental)",        123.0, 43),
    # RETIRADO del examen: "Till There Was You (Vanilla Ace)" (b3f57c3c) tiene
    # jitter p90 de 14.6 ms y tempo real ~123.04 (deriva): su propia fase depende
    # del BPM asumido, asi que RECHAZA los criterios del golden set y no sirve
    # como referencia. Reponer el tercer par cuando se certifique un reemplazo.
]

# El ancla del examen se compara MODULO el periodo de beat: el valor absoluto que
# reporta el worker (p. ej. 16284.3 ms) es el mismo ancla + n*beat.
GOLDEN_PAIRS = [(0, 1), (2, 3)]  # indices (deck A, deck B); el orden fija el signo.
# El tercer par quedo pendiente al retirar "Till There Was You" (dato malo).

# Prueba CIEGA (v6.2): tracks jamas calibrados por oido. El examen imprime sus
# anclas calculadas (no hay gold contra el cual comparar); se escriben a mano
# via SQL y el DJ las valida alineando por rejilla en el mixer.
BLIND_TRACKS = [
    ("a83916eb-2333-43e8-b131-77071032db59", "This Sound (Extended Mix)",  124.0),
    ("cf7f1585-f6cf-451a-bf12-e4ebe01c8d89", "Day 'N' Nite (Extended Mix)", 124.0),
]

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


def detect_cues(y: np.ndarray, sr: int, bpm, first_beat_ms):
    """8 cue points sobre estructura + energía, snapeados a frase de 16 compases."""
    if not bpm or bpm < 40 or first_beat_ms is None:
        return None
    try:
        beat_ms = 60000.0 / bpm
        bar_ms = beat_ms * 4
        dur_ms = (len(y) / sr) * 1000.0

        # envolvente RMS por compás
        hop = 512
        rms = librosa.feature.rms(y=y, hop_length=hop)[0]
        times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop) * 1000.0
        # bandas por frame
        S = np.abs(librosa.stft(y, n_fft=2048, hop_length=hop))
        freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
        bass = S[freqs < 200, :].sum(axis=0)
        high = S[freqs >= 2000, :].sum(axis=0)

        n_bars = int(max(0, (dur_ms - first_beat_ms) // bar_ms))
        if n_bars < 4:
            return None

        def frame_val(arr, t0, t1):
            m = (times >= t0) & (times < t1)
            return float(np.mean(arr[m])) if m.any() else 0.0

        full_b = np.array([frame_val(rms, first_beat_ms + b * bar_ms, first_beat_ms + (b + 1) * bar_ms) for b in range(n_bars)])
        bass_b = np.array([frame_val(bass, first_beat_ms + b * bar_ms, first_beat_ms + (b + 1) * bar_ms) for b in range(n_bars)])
        high_b = np.array([frame_val(high, first_beat_ms + b * bar_ms, first_beat_ms + (b + 1) * bar_ms) for b in range(n_bars)])
        full_b = _norm_max(full_b); bass_b = _norm_max(bass_b); high_b = _norm_max(high_b)

        # segmentación estructural (novedad sobre self-similarity de MFCC/chroma)
        try:
            seg_hop = max(hop, sr // 2)  # ~0.5s por frame → segmentación rápida
            feat = librosa.feature.mfcc(y=y, sr=sr, hop_length=seg_hop, n_mfcc=13)
            bounds = librosa.segment.agglomerative(feat, min(8, max(2, n_bars // 8)))
            bound_ms = librosa.frames_to_time(bounds, sr=sr, hop_length=seg_hop) * 1000.0
            boundaries = sorted(set(int(round((bm - first_beat_ms) / bar_ms)) for bm in bound_ms if bm > first_beat_ms))
            boundaries = [b for b in boundaries if 0 <= b < n_bars]
        except Exception:
            boundaries = []

        phrase_bars = 16 if n_bars >= 32 else 8
        phrase_ms = phrase_bars * bar_ms

        # ── CAMBIO 3 (v5.1) — cuantizar a 4 compases, no a 16 ────────────────
        # ANTES: snap a frase de 16 compases. Eso puede MOVER un cue hasta 8
        # compases (~15 s a 128 BPM): se detectaba bien la frontera y después se
        # la corría quince segundos.
        # Además contradecía la medición del propio catálogo: 91% de las
        # distancias entre cues son múltiplos de 8 compases y 96% de 4 — forzar
        # todo a múltiplos de 16 no reproduce esa distribución.
        # AHORA: snap a 4 compases (error máximo 2 compases, ~3.75 s a 128 BPM).
        # Corrige el error de detección sin reubicar el cue. Los múltiplos de
        # 8/16 aparecen solos, porque así está construida la música.
        SNAP_BARS = 4
        snap_ms = SNAP_BARS * bar_ms

        def snap(ms):
            return first_beat_ms + round((ms - first_beat_ms) / snap_ms) * snap_ms

        chosen = {}

        def add(num, ms):
            s = snap(ms)
            if s < 0 or s >= dur_ms:
                return
            for k, v in chosen.items():
                if k != num and abs(v - s) < 8 * bar_ms:
                    return
            if num in chosen:
                return
            chosen[num] = int(round(s))

        def bar_to_ms(b):
            return first_beat_ms + b * bar_ms

        def jump_at(b):
            if b < 4 or b + 4 > n_bars:
                return -1e9
            return float(np.mean(full_b[b:b + 4]) - np.mean(full_b[b - 4:b]))

        # ---- Asignación por CAMBIOS DE ENERGÍA (metodología DeepMancho refinada) ----
        # Reglas (German): A = inicio real, B..G = SOLO en cambios de energía reales
        # (sube/baja considerablemente), H = último punto alto justo antes de la caída
        # sostenida hasta el final. Nada en zona plana ni a media bajada.
        THRESH = 0.15   # cambio de energía mínimo (0-1) para marcar un cue
        HIGH = 0.5      # umbral de "parte alta"

        def _win(arr, a, b):
            a = max(0, int(a)); b = min(n_bars, int(b))
            if b <= a:
                return 0.0
            return float(np.mean(arr[a:b]))

        def win(a, b):          # energía total (banda ancha)
            return _win(full_b, a, b)

        def winb(a, b):         # energía de GRAVES (kick + bajo)
            return _win(bass_b, a, b)

        # ── CAMBIO 2 (v5.1) — la banda de graves entra en la decisión ────────
        # ANTES: `bass_b` y `high_b` se calculaban y NO se usaban en ningún cue;
        # todo se decidía con `full_b` (RMS de banda ancha).
        # Ese es justo el dato que NO captura el criterio real: en un breakdown
        # el kick desaparece (el waveform de Rekordbox se pone verde) pero si
        # los medios siguen fuertes, el RMS de banda ancha casi no se mueve y el
        # cambio no se marca.
        # AHORA: el cambio se mide como el promedio del salto en banda ancha y
        # del salto en graves. Una caída SOLO de graves (kick que sale) ya
        # alcanza para marcar el cue, que es la regla que se quería.
        def delta(b):  # +sube / -baja (compara 4 compases antes/después)
            d_full = win(b, b + 4) - win(b - 4, b)
            d_bass = winb(b, b + 4) - winb(b - 4, b)
            return 0.5 * d_full + 0.5 * d_bass

        # A (0) — inicio real: primer compás con energía sostenida
        a_bar = 0
        for b in range(n_bars):
            if win(b, b + 4) > 0.12:
                a_bar = b
                break
        add(0, bar_to_ms(a_bar))

        # ── CAMBIO 1 (v5.1) — CAUSA RAÍZ del MIX-OUT al ~95% ────────────────
        # EL BUG: la búsqueda arrancaba en `b = n_bars - 4` y medía la cola como
        # `win(b + 4, n_bars)` = `win(n_bars, n_bars)` = 0.0 — una ventana VACÍA.
        # Con tail=0.0, la condición `tail < here - 0.12` se cumple siempre que
        # el compás tenga algo de energía, así que la PRIMERA iteración acertaba
        # y H quedaba a 4 compases del final, en todos los tracks. Ese es el 95%
        # sistemático que hubo que pisar a mano con el 78.4%.
        #
        # EL ARREGLO, tres condiciones:
        #   1. La cola es una ventana REAL (mínimo TAIL_BARS compases).
        #   2. La caída se SOSTIENE hasta el final (se mira el promedio de toda
        #      la cola Y los últimos 8 compases, para que no valga una bajada
        #      momentánea seguida de un repunte).
        #   3. Queda RUNWAY suficiente después de H para mezclar (>= RUNWAY_BARS).
        #      Esto ataca de raíz los `h_cue_runway_override` (hubo cues con
        #      0.13 s de margen; el guardrail los atrapaba, pero el dato nacía mal).
        TAIL_BARS = 16      # cola mínima a evaluar (~30 s a 128 BPM)
        RUNWAY_BARS = 8     # audio mínimo después de H (~15 s a 128 BPM)

        h_bar = -1
        b = n_bars - TAIL_BARS
        while b > a_bar + phrase_bars:
            here = win(b - 4, b)                  # energía justo ANTES del punto
            tail = win(b, n_bars)                 # todo lo que queda
            tail_end = win(n_bars - 8, n_bars)    # el final propiamente dicho
            if here >= HIGH and tail < here - 0.12 and tail_end < here - 0.12:
                h_bar = b
                break
            b -= 4

        if h_bar < 0:
            # Fallback honesto: si la caída no se detecta (p. ej. el track
            # termina de golpe a plena energía), se usa la mediana medida del
            # catálogo (78.4% de la duración). Es ubicar por porcentaje, con la
            # incertidumbre conocida de ±30 s — por eso es SOLO el fallback,
            # nunca el camino principal.
            h_bar = int(round(n_bars * 0.784))

        h_bar = max(a_bar + phrase_bars, min(h_bar, n_bars - RUNWAY_BARS))
        add(7, bar_to_ms(h_bar))

        # Cambios significativos entre A y H (resolución de media frase)
        cands = []
        for b in range(a_bar + 4, max(a_bar + 5, h_bar - 4), 4):
            d = delta(b)
            if abs(d) >= THRESH:
                cands.append((b, d))
        # dedupe por cercanía (mín 8 compases), conservando el cambio más fuerte
        cands.sort(key=lambda x: x[0])
        filtered = []
        for cb, cd in cands:
            if filtered and (cb - filtered[-1][0]) < 8:
                if abs(cd) > abs(filtered[-1][1]):
                    filtered[-1] = (cb, cd)
                continue
            filtered.append((cb, cd))
        # si hay más de 6, conserva los 6 cambios más fuertes y reordénalos por tiempo
        if len(filtered) > 6:
            filtered = sorted(sorted(filtered, key=lambda x: -abs(x[1]))[:6], key=lambda x: x[0])

        # Asigna B..G (1..6) en orden de tiempo; sólo avanza el número si el cue entró
        num = 1
        for cb, cd in filtered:
            if num > 6:
                break
            before = len(chosen)
            add(num, bar_to_ms(cb))
            if len(chosen) > before:
                num += 1

        result = []
        for num in sorted(chosen.keys()):
            label, color = CUE_DEF[num]
            result.append({"number": num, "label": label, "color": color, "positionMs": chosen[num], "type": "cue"})
        return result or None
    except Exception:
        traceback.print_exc()
        return None


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


def analyze(path: str, bpm_seed=None) -> dict:
    y, sr = librosa.load(path, sr=SR, mono=True, duration=MAX_DURATION)
    if y.size == 0:
        raise RuntimeError("audio vacío")
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
    cues = detect_cues(y, sr, bpm, first_beat_ms)
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
                out["bpm_fine"] = round(bpm_fino - round(bpm_ref), 3)
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
    try:
        import pyloudnorm  # dependencia: pyloudnorm>=0.1 (requirements)
    except ImportError:
        print("WARN CM1: pyloudnorm no instalado; loudness_lufs no se calcula", flush=True)
        return None
    try:
        y44, sr44 = librosa.load(path, sr=44100, mono=True, duration=MAX_DURATION)
        if y44.size == 0:
            return None
        meter = pyloudnorm.Meter(sr44)
        lufs = float(meter.integrated_loudness(y44))
        if not np.isfinite(lufs):
            return None
        return round(lufs, 2)
    except Exception:
        traceback.print_exc()
        return None


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
    ataques = np.empty(len(pk)); pesos = props["peak_heights"].astype(np.float64)
    for i, p in enumerate(pk):
        th = 0.25 * env[p]
        j, lo = p, max(0, p - lim_atras)
        while j > lo and env[j] > th:
            j -= 1
        ataques[i] = j / sr

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


def rendition_url(track_id: str) -> str:
    """URL del MISMO audio que reproduce el navegador (stream-track)."""
    return f"{WORKER_API_URL}/stream-track?track_id={track_id}&format=aac"


def _wrap(x: float, T: float) -> float:
    x = x % T
    return x - T if x > T / 2 else x


def golden_exam():
    """Examen del golden set. Solo LEE audio e imprime; NUNCA escribe en la base.
    Gate: |error relativo| <= ANCHOR_TOL_MS en los 3 pares -> APROBADO."""
    print("[CM2 EXAMEN] arrancando examen del golden set (6 tracks, solo lectura)...", flush=True)
    resultados = {}
    for i, (tid, title, bpm, gold) in enumerate(GOLDEN_TRACKS):
        try:
            url = rendition_url(tid)
            r = requests.get(url, timeout=180)
            r.raise_for_status()
            tmp = tempfile.NamedTemporaryFile(suffix=".m4a", delete=False)
            tmp.write(r.content); tmp.close()
            res = compute_anchor(tmp.name, bpm)
            os.remove(tmp.name)
            res["gold"] = gold; res["bpm"] = bpm
            resultados[i] = res
            flag = " ⚠ residuo alto" if res["residuo_ms"] > 8 else ""
            print(f"[CM2 EXAMEN] {title}: ancla={res['ancla_ms']}ms "
                  f"bpm_real={res['bpm_real']} residuo={res['residuo_ms']}ms{flag}", flush=True)
        except Exception as e:
            print(f"[CM2 EXAMEN] {title}: FALLO al analizar ({e})", flush=True)
    aprobado = True
    for a, b in GOLDEN_PAIRS:
        if a not in resultados or b in (None,) or b not in resultados:
            print(f"[CM2 EXAMEN] Par {GOLDEN_TRACKS[a][1]} × {GOLDEN_TRACKS[b][1]}: SIN DATOS", flush=True)
            aprobado = False
            continue
        A, B = resultados[a], resultados[b]
        T = 60000.0 / ((A["bpm"] + B["bpm"]) / 2.0)
        err = _wrap((B["ancla_ms"] - A["ancla_ms"]) - (B["gold"] - A["gold"]), T)
        ok = abs(err) <= ANCHOR_TOL_MS
        aprobado = aprobado and ok
        print(f"[CM2 EXAMEN] Par {GOLDEN_TRACKS[a][1]} × {GOLDEN_TRACKS[b][1]}: "
              f"error {err:+.1f} ms {'✅' if ok else '❌'}", flush=True)
    for tid, title, bpm in BLIND_TRACKS:
        try:
            r = requests.get(rendition_url(tid), timeout=180)
            r.raise_for_status()
            tmp = tempfile.NamedTemporaryFile(suffix=".m4a", delete=False)
            tmp.write(r.content); tmp.close()
            res = compute_anchor(tmp.name, bpm)
            os.remove(tmp.name)
            print(f"[CM2 CIEGA] {title}: ancla={res['ancla_ms']}ms "
                  f"bpm_real={res['bpm_real']} residuo={res['residuo_ms']}ms", flush=True)
        except Exception as e:
            print(f"[CM2 CIEGA] {title}: FALLO ({e})", flush=True)
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
    return job, data.get("track") or {}, data.get("audio_url"), data.get("rendition_upload")


def make_rendition(src_path: str):
    """Convierte a m4a/AAC 192k (reproducible en navegador). Devuelve ruta o None."""
    try:
        out = src_path + ".stream.m4a"
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", src_path, "-vn", "-c:a", "aac", "-b:a", "192k",
             "-movflags", "+faststart", out],
            capture_output=True, timeout=180,
        )
        if proc.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
            return None
        return out
    except Exception:
        return None


def upload_rendition(signed_url: str, m4a_path: str) -> bool:
    """Sube el m4a a la URL firmada de subida de Supabase Storage."""
    try:
        with open(m4a_path, "rb") as f:
            data = f.read()
        r = requests.put(
            signed_url,
            data=data,
            headers={"content-type": "audio/mp4", "x-upsert": "true"},
            timeout=180,
        )
        return r.status_code in (200, 201)
    except Exception:
        return False


def download_audio(audio_url: str) -> str:
    r = requests.get(audio_url, timeout=120)
    r.raise_for_status()
    ext = os.path.splitext(audio_url.split("?")[0])[1] or ".audio"
    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    tmp.write(r.content)
    tmp.close()
    return tmp.name


def send_result(job_id: str, track_id: str, status: str, result: dict = None, error: str = None):
    payload = {"job_id": job_id, "track_id": track_id, "status": status}
    if result is not None:
        payload["result"] = result
    if error:
        payload["error"] = error[:1000]
    r = requests.post(f"{WORKER_API_URL}/worker-result", headers=HEADERS, json=payload, timeout=60)
    r.raise_for_status()


# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------
def process_job(job: dict, track: dict, audio_url: str, rendition_upload: dict = None):
    job_id = job["id"]
    track_id = job["track_id"]
    print(f"[job {job_id}] track {track_id} — analizando...", flush=True)
    tmp = None
    try:
        if not audio_url:
            send_result(job_id, track_id, "error", error="track sin audio")
            return
        tmp = download_audio(audio_url)
        result = analyze(tmp, bpm_seed=track.get("bpm"))
        # CM1-bis: loudness restaurado (la v5 lo habia perdido — regresion detectada 18-ago)
        lufs = compute_loudness_lufs(tmp)
        if lufs is not None:
            result["loudness_lufs"] = lufs
        # CM2 (solo con ENABLE_ANCHOR_BACKFILL=true y examen aprobado): ancla de
        # precision sobre la RENDITION (lo que oye el DJ), nunca sobre el master.
        # Escribe SOLO first_beat_detected_ms; jamas first_beat_offset_ms ni _source.
        if ENABLE_ANCHOR_BACKFILL:
            try:
                bpm_ref = track.get("bpm") or result.get("bpm")
                if bpm_ref and 40 < float(bpm_ref) < 240:
                    rr = requests.get(rendition_url(track_id), timeout=180)
                    rr.raise_for_status()
                    rtmp = tempfile.NamedTemporaryFile(suffix=".m4a", delete=False)
                    rtmp.write(rr.content); rtmp.close()
                    anc = compute_anchor(rtmp.name, float(bpm_ref))
                    os.remove(rtmp.name)
                    if anc["residuo_ms"] <= 8:
                        result["first_beat_detected_ms"] = int(round(anc["ancla_ms"]))
                        print(f"[job {job_id}] CM2 ancla={anc['ancla_ms']}ms residuo={anc['residuo_ms']}ms", flush=True)
                    else:
                        print(f"[job {job_id}] CM2 residuo alto ({anc['residuo_ms']}ms) — ancla NO escrita", flush=True)
            except Exception as e:
                print(f"[job {job_id}] CM2 fallo (no bloquea el job): {e}", flush=True)
        # Rendition: si la canción no tiene versión reproducible (ej. AIFF), conviértela a
        # m4a/AAC y súbela vía la URL firmada; el backend fija stream_asset_path.
        if rendition_upload and rendition_upload.get("url") and rendition_upload.get("path"):
            m4a = make_rendition(tmp)
            if m4a:
                if upload_rendition(rendition_upload["url"], m4a):
                    result["rendition_path"] = rendition_upload["path"]
                    print(f"[job {job_id}] rendition subida: {rendition_upload['path']}", flush=True)
                else:
                    print(f"[job {job_id}] WARN: no se pudo subir la rendition", flush=True)
                try:
                    os.remove(m4a)
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
        print(f"[job {job_id}] OK — cues={n_cues} energy={result['energy']}", flush=True)
    except Exception as e:
        traceback.print_exc()
        try:
            send_result(job_id, track_id, "error", error=str(e))
        except Exception:
            pass
        print(f"[job {job_id}] FALLO: {e}", flush=True)
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


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


def render_set(job, tracks, upload_url, result_path):
    """Mezcla el set completo y lo sube. Devuelve (duracion_s, tracklist)."""
    tmpdir = tempfile.mkdtemp(prefix="dm_set_")
    salida = np.zeros((0, 2), dtype=np.float32)
    tracklist, bpm_set, fin_ant, fase_ant = [], None, 0, 0.0

    for i, tr in enumerate(tracks):
        titulo = tr.get("title") or "?"
        print(f"  [{i+1}/{len(tracks)}] {titulo}", flush=True)
        r = requests.get(tr["audio_url"], timeout=300)
        r.raise_for_status()
        p = os.path.join(tmpdir, f"{i}.audio")
        with open(p, "wb") as f:
            f.write(r.content)

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

    # Masterizado: loudness parejo + techo de true peak
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
    return dur, tracklist


def _tl(tr):
    return {"track_id": tr.get("id") or tr.get("track_id"),
            "title": tr.get("title"), "artist": tr.get("artist"),
            "label": tr.get("label")}


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
            _set_api("fail", {"job_id": job["id"], "error": str(e)[:2000]})
        except Exception:
            pass
    return True


def main():
    print("DeepMancho worker iniciado (v7.2.1: ancla al ATAQUE del kick + downbeat por snare + cues en la rejilla nueva + grid_confidence + examen recalibrado). Esperando jobs...", flush=True)
    if ENABLE_SET_RENDER:
        print("[set-render] habilitado — se atenderan jobs de render de sets", flush=True)
    if GOLDEN_EXAM:
        try:
            golden_exam()
        except Exception:
            traceback.print_exc()
            print("[CM2 EXAMEN] el examen fallo pero el worker sigue normal", flush=True)
    idle = 0
    while True:
        try:
            job, track, audio_url, rendition_upload = next_job()
        except Exception as e:
            print(f"next_job error: {e}", flush=True)
            time.sleep(POLL_INTERVAL)
            continue
        if job:
            idle = 0
            process_job(job, track, audio_url, rendition_upload)
        else:
            # Sin jobs de analisis: aprovechar para renderizar sets si hay cola.
            # El analisis tiene prioridad (un track sin analizar bloquea mas que
            # un set sin renderizar).
            if ENABLE_SET_RENDER and poll_set_render():
                idle = 0
                continue
            idle += 1
            if idle % 12 == 1:
                print("sin jobs pendientes...", flush=True)
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
