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
GOLDEN_EXAM = os.environ.get("GOLDEN_EXAM", "true").lower() != "false"
ENABLE_ANCHOR_BACKFILL = os.environ.get("ENABLE_ANCHOR_BACKFILL", "").lower() == "true"
ANCHOR_SR = 22050    # SR del análisis de ancla (independiente del SR=11025 general)
ANCHOR_HOP = 128     # ~5.8 ms por frame de onset a 22050 Hz
ANCHOR_TOL_MS = 10.0 # criterio del examen (error relativo por par)

# Golden set — anclas validadas por oído + telemetría (18-ago-2026).
# gold_ms = first_beat_detected_ms vigente en la base tras la calibración manual.
GOLDEN_TRACKS = [
    ("b411743d-de03-4190-b6fe-f44aa6685ba8", "Make It Hot (Mustafa Ismaeel Rmx)", 122.0, 81),
    ("a16963a1-0d15-4354-80e5-ba27500dd7b1", "Blame (Claptone Extended Mix)",     122.0, 128),
    ("4cc427fb-03a6-4165-8ca3-2025b6ebe779", "No Time for Tears (Original Mix)",  122.0, 238),
    ("7d377de8-6562-416d-8b0b-97317f9b6c7f", "Slip Away (Original Mix)",          122.0, 181),
    ("cfaaaa0e-26d1-4e96-ab3c-8a5a49f34f07", "Right Thing (Instrumental)",        123.0, 398),
    ("b3f57c3c-7b58-49de-9dd8-c5555aab6909", "Till There Was You (Vanilla Ace)",  123.0, 372),
]
GOLDEN_PAIRS = [(0, 1), (2, 3), (4, 5)]  # índices (deck A, deck B) — el orden fija el signo

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


def analyze(path: str) -> dict:
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

    return {
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
    """Ancla de rejilla (primer downbeat audible, en ms) sobre el archivo dado.
    Usa el BPM de la base como semilla (fuente de verdad protegida: no re-decide
    el tempo, solo lo afina en ±fracción mínima con el ajuste de fase).
    Devuelve dict(ancla_ms, bpm_real, residuo_ms) o lanza excepción."""
    y, sr = librosa.load(path, sr=ANCHOR_SR, mono=True, duration=MAX_DURATION)
    if y.size == 0:
        raise RuntimeError("CM2: audio vacío")
    periodo_s = 60.0 / float(bpm)
    # Onsets (transientes) banda completa + banda grave (kick) — NO envolventes RMS:
    # el bajo en contratiempo del house corre el centro de energía; los onsets no.
    onset_full = librosa.onset.onset_strength(y=y, sr=sr, hop_length=ANCHOR_HOP)
    onset_bass = librosa.onset.onset_strength(y=y, sr=sr, hop_length=ANCHOR_HOP, fmax=160)
    times = librosa.times_like(onset_full, sr=sr, hop_length=ANCHOR_HOP)

    f_real, b, resid_ms = _ajuste_lineal_fase(
        _fase_por_segmento(onset_full, times, periodo_s), periodo_s)
    periodo_real = 1.0 / f_real
    t_beat0 = (b / (2 * np.pi)) * periodo_real % periodo_real

    # Downbeat: plegar el onset GRAVE módulo compás (4 beats) sobre la rejilla hallada
    compas = 4.0 * periodo_real
    pos = (times - t_beat0) % compas
    slot = np.floor(pos / periodo_real).astype(int) % 4
    energia_slot = np.array([onset_bass[slot == k].sum() for k in range(4)])
    t_down0 = (t_beat0 + int(np.argmax(energia_slot)) * periodo_real) % compas

    # Ancla = primer downbeat despues del arranque audible
    umbral = 0.1 * np.percentile(onset_full, 95)
    activos = np.nonzero(onset_full > umbral)[0]
    t_inicio = float(times[activos[0]]) if len(activos) else 0.0
    k = math.ceil(max(0.0, (t_inicio - 1e-3) - t_down0) / compas)
    ancla_s = t_down0 + k * compas
    return dict(ancla_ms=round(ancla_s * 1000.0, 1),
                bpm_real=round(60.0 / periodo_real, 3),
                residuo_ms=round(resid_ms, 1))


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
        result = analyze(tmp)
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


def main():
    print("DeepMancho worker iniciado (v6: CM1-bis loudness + CM2 ancla/examen golden set). Esperando jobs...", flush=True)
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
            idle += 1
            if idle % 12 == 1:
                print("sin jobs pendientes...", flush=True)
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
