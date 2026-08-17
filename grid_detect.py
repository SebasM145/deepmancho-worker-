"""
Detector de rejilla — metodología derivada de Rekordbox (v5).

NO es una reimplementación teórica: las tres reglas de abajo se midieron sobre
729 rejillas reales exportadas de Rekordbox del catálogo de DeepMancho.

  REGLA 1 — El ancla siempre cabe dentro de UN beat desde el inicio del archivo.
            729/729 casos. Mediana: 3.5% de un beat (17 ms). Máximo: 1.00 beat.
            Consecuencia: NO hay que buscar el downbeat en toda la pista. El
            espacio de búsqueda es ~700 veces más chico, y eso es justo lo que
            hacía fallar al detector anterior (buscaba en todo el track y se
            enganchaba a un beat equivocado, dando 114 ms de error medio).

  REGLA 2 — El tempo es entero. 704/729 exactos (96%). Los 25 restantes son
            enteros con ruido de medición (118.01, 124.98, 127.01...), no
            tempos genuinamente fraccionarios.

  REGLA 3 — Los cue points caen exactamente sobre la rejilla. 656/656 a menos
            del 2% de un beat. La rejilla es el marco de referencia.

Criterio de éxito, definido antes de escribir el código:
  error mediano del ancla < 10 ms contra las rejillas de Rekordbox.
  (Punto de partida del detector viejo: 54 ms mediano.)
"""
import numpy as np
import librosa

HOP = 512
ONSET_FOOT_FRACTION = 0.35   # centro de la meseta estable (0.20-0.50 dan igual)
SR_ANALYSIS = 22050


# ─────────────────────────────────────────────────────────────────────
# PASO 1 — TEMPO
# ─────────────────────────────────────────────────────────────────────

def _onset_env(y, sr):
    env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP,
                                       aggregate=np.median)
    m = float(np.max(env))
    return env / m if m > 0 else env


def _grid_score(env, sr, period_s, phase_s, dur_s):
    """Energía de onsets que cae sobre una rejilla (período, fase)."""
    n = int((dur_s - phase_s) / period_s)
    if n < 16:
        return -1e9
    frames = np.round((phase_s + np.arange(n) * period_s) * sr / HOP).astype(int)
    frames = frames[(frames >= 0) & (frames < len(env))]
    if len(frames) < 16:
        return -1e9
    lo = np.maximum(frames - 1, 0)
    hi = np.minimum(frames + 2, len(env))
    return float(np.mean([np.max(env[a:b]) for a, b in zip(lo, hi)]))


def detect_tempo(y, sr, seed_bpm=None, env=None):
    """
    Tempo por ajuste global sobre toda la pista, con redondeo a entero
    (REGLA 2). Devuelve (bpm, env).
    """
    if env is None:
        env = _onset_env(y, sr)
    dur_s = len(y) / sr

    if seed_bpm is None or not (60 <= seed_bpm <= 200):
        t, _ = librosa.beat.beat_track(y=y, sr=sr, trim=False)
        seed_bpm = float(np.atleast_1d(t)[0]) if np.atleast_1d(t).size else 126.0
        # corrección de octava: librosa cae en sub/super armónicos
        while seed_bpm < 90:
            seed_bpm *= 2
        while seed_bpm > 180:
            seed_bpm /= 2

    # búsqueda gruesa ±4 BPM
    best = (seed_bpm, -1e9)
    for bpm in np.arange(seed_bpm - 4.0, seed_bpm + 4.0, 0.05):
        if not (60 <= bpm <= 200):
            continue
        p = 60.0 / bpm
        s = max(_grid_score(env, sr, p, ph, dur_s)
                for ph in np.arange(0, p, p / 8))
        if s > best[1]:
            best = (bpm, s)

    # refinamiento fino
    coarse = best[0]
    best_f = (coarse, -1e9)
    for bpm in np.arange(coarse - 0.06, coarse + 0.06, 0.004):
        p = 60.0 / bpm
        s = max(_grid_score(env, sr, p, ph, dur_s)
                for ph in np.arange(0, p, p / 16))
        if s > best_f[1]:
            best_f = (bpm, s)

    bpm = float(best_f[0])

    # REGLA 2: si está cerca de un entero, ES ese entero.
    if abs(bpm - round(bpm)) <= 0.15:
        bpm = float(round(bpm))

    return round(bpm, 2), env


# ─────────────────────────────────────────────────────────────────────
# PASO 2 — ANCLA (el cambio clave respecto a v4)
# ─────────────────────────────────────────────────────────────────────

def detect_anchor(y, sr, bpm):
    """
    REGLA 1: el ancla está dentro del PRIMER BEAT del archivo.

    En vez de buscar la fase en toda la pista (que es donde el detector
    anterior se perdía), se busca el ataque de graves más marcado dentro
    de la ventana [0, un beat], con resolución de muestra.

    Devuelve el ancla en milisegundos.
    """
    beat = 60.0 / bpm
    win_end = int(min(len(y), (beat * 1.05) * sr))   # 5% de margen
    if win_end < int(sr * 0.05):
        return 0

    seg = y[:win_end].astype(np.float64)

    # Envolvente de graves en el dominio del tiempo (donde vive el bombo).
    # Se evita el espectrograma a propósito: su ventana "unta" la energía y
    # desplaza el ataque decenas de ms — ese fue un error medido en v4.
    try:
        from scipy.signal import butter, sosfiltfilt
        sos = butter(4, 200.0, btype='low', fs=sr, output='sos')
        low = np.abs(sosfiltfilt(sos, seg))
    except Exception:
        low = np.abs(seg)

    # suavizado corto (~2 ms), sin desplazar la fase
    w = max(3, int(sr * 0.002))
    envt = np.convolve(low, np.ones(w) / w, mode='same')

    # El ataque es donde la envolvente SUBE más rápido.
    d = np.diff(envt, prepend=envt[0])
    if np.max(d) <= 0:
        return 0

    peak = int(np.argmax(d))

    # Retroceder hasta el PIE de la subida: el golpe empieza donde la
    # envolvente toca su mínimo local antes del ataque, no en el punto de
    # máxima pendiente. Medido: sin este retroceso queda un sesgo sistemático
    # de +13 ms constante (6 de 8 casos de prueba dieron exactamente +13).
    #
    # Se busca el mínimo dentro de una ventana previa razonable (30 ms), en
    # vez de usar un umbral relativo: con reverb la envolvente nunca baja lo
    # suficiente y el umbral no dispara.
    back = min(peak, int(sr * 0.030))
    if back > 2:
        seg_prev = envt[peak - back:peak + 1]
        i_min = peak - back + int(np.argmin(seg_prev))
        base = float(envt[i_min])
        top = float(envt[peak])
        # El golpe "empieza" donde la envolvente supera el pie por una
        # fracción de su subida total. Calibrado contra las pruebas:
        #   - tomar el punto de máxima pendiente daba +13 ms
        #   - tomar el mínimo local daba -12 ms
        # El cruce del 25% de la subida cae entre ambos.
        thr = base + (top - base) * ONSET_FOOT_FRACTION
        i = i_min
        while i < peak and envt[i] < thr:
            i += 1
    else:
        i = peak

    anchor_s = i / sr
    if anchor_s >= beat:
        anchor_s -= beat
    return int(round(max(0.0, anchor_s) * 1000))


# ─────────────────────────────────────────────────────────────────────
# Punto de entrada
# ─────────────────────────────────────────────────────────────────────

def detect_grid(y, sr, seed_bpm=None):
    """Devuelve (bpm, ancla_ms) siguiendo la metodología de Rekordbox."""
    bpm, env = detect_tempo(y, sr, seed_bpm)
    anchor_ms = detect_anchor(y, sr, bpm)
    return bpm, anchor_ms
