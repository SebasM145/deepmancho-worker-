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
SEED_MARGIN = 0.08          # búsqueda de tempo: ±8 % alrededor de la semilla de librosa


# ─────────────────────────────────────────────────────────────────────
# PASO 1 — TEMPO
# ─────────────────────────────────────────────────────────────────────

def _onset_env(y, sr):
    env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP,
                                       aggregate=np.median)
    m = float(np.max(env))
    return env / m if m > 0 else env


def _max3(env):
    """env3[f] = max(env[f-1 .. f+1]), recortado en los bordes: la ventana de _grid_score."""
    from scipy.ndimage import maximum_filter1d
    return maximum_filter1d(env, size=3, mode="nearest")


def _grid_score(env, sr, period_s, phase_s, dur_s, env3=None):
    """Energía de onsets que cae sobre una rejilla (período, fase).

    Mismo resultado que el máximo de env[f-1:f+2] golpe por golpe, pero con la
    ventana precalculada (`env3`): ese bucle de Python era el 60 % de `analyze`
    (10.700 llamadas en un tema de 10 min)."""
    n = int((dur_s - phase_s) / period_s)
    if n < 16:
        return -1e9
    frames = np.round((phase_s + np.arange(n) * period_s) * sr / HOP).astype(int)
    frames = frames[(frames >= 0) & (frames < len(env))]
    if len(frames) < 16:
        return -1e9
    if env3 is None:
        env3 = _max3(env)
    return float(np.mean(env3[frames]))


OCTAVAS = (1.0, 2.0, 0.5, 4.0, 0.25)
PROPORCIONES = (1.5, 2 / 3, 4 / 3, 0.75)
# Una semilla que no es octava solo gana si su rejilla puntua al menos 10 % mas que la
# mejor octava (golden set 2-oct: Right Thing +26 % con x1,5 y es correcta; Day 'N' Nite
# +1,6 % con x0,75 y es incorrecta).
VENTAJA_PROPORCION = 1.10
# #206 (4-oct, 136 BPM sin etiqueta → 90,54): con bombo en cada tiempo y hi-hat a contratiempo
# hay un golpe cada MEDIO tiempo, y la rejilla de 2/3 del tempo (1,5 tiempos = 3 medios
# tiempos) cae siempre sobre uno. Esa candidata no aporta nada que la octava no explique.
TOLERANCIA_MEDIA_REJILLA = 0.01


def explicada_por_media_rejilla(cand_bpm, octava_bpm, tol=TOLERANCIA_MEDIA_REJILLA):
    """¿El período de la candidata es un múltiplo entero del MEDIO tiempo de la octava?

    Si lo es, todos sus golpes caen sobre la rejilla de medio tiempo de la octava: lo que
    puntúa la candidata ya lo explica la octava y no puede ganarle. Ejemplos:
      · 136 contra 90,54 (×2/3): 2·136/90,54 = 3,004 medios tiempos → explicada.
      · «Right Thing» (2-oct): 123 contra 174,33 (la octava de 80,75): 2,835 → compite.
      · ×4/3 y ×0,75 dan 1,5 y 2,667 medios tiempos → compiten como hasta ahora.
    """
    if not (cand_bpm and octava_bpm) or cand_bpm <= 0 or octava_bpm <= 0:
        return False
    r = 2.0 * octava_bpm / cand_bpm  # período de la candidata en medios tiempos de la octava
    k = round(r)
    return k >= 1 and abs(r - k) <= tol * k




def semillas_candidatas(cruda):
    """Semillas a probar a partir de la de librosa, todas entre 90 y 180 BPM.

    librosa cae en sub/super armónicos: el doble y la mitad (corregidos desde siempre)
    y también 2/3 o 3/4 del tempo real. Ejemplo del 2-oct: «Right Thing» (123 BPM)
    da 80,75 = 2/3 · 123. Solo con duplicar salía 161,5 → 174,33. Con ×1,5 entra 121
    y la puntuación de la rejilla elige 123. Devuelve (octavas, otras_proporciones).
    """
    octavas, otras = [], []
    for f in OCTAVAS + PROPORCIONES:
        c = cruda * f
        if 90 <= c <= 180 and all(abs(c - o) / o > 0.03 for o in octavas + otras):
            (octavas if f in OCTAVAS else otras).append(c)
    if not octavas:  # muy fuera de rango: la corrección de octava de siempre
        c = cruda
        while c < 90:
            c *= 2
        while c > 180:
            c /= 2
        octavas = [c]
    return octavas, otras


def _busqueda_gruesa(env, sr, dur_s, semilla, env3=None):
    """Mejor (bpm, puntuación) en ±8 % de la semilla, paso 0,05."""
    if env3 is None:
        env3 = _max3(env)
    margen = max(4.0, semilla * SEED_MARGIN)
    best = (semilla, -1e9)
    for bpm in np.arange(semilla - margen, semilla + margen, 0.05):
        if not (60 <= bpm <= 200):
            continue
        p = 60.0 / bpm
        sc = max(_grid_score(env, sr, p, ph, dur_s, env3) for ph in np.arange(0, p, p / 8))
        if sc > best[1]:
            best = (bpm, sc)
    return best


def detect_tempo(y, sr, seed_bpm=None, env=None):
    """
    Tempo por ajuste global sobre toda la pista, con redondeo a entero
    (REGLA 2). Devuelve (bpm, env).
    """
    if env is None:
        env = _onset_env(y, sr)
    dur_s = len(y) / sr
    env3 = _max3(env)

    semillas = [seed_bpm]
    if seed_bpm is None or not (60 <= seed_bpm <= 200):
        t, _ = librosa.beat.beat_track(y=y, sr=sr, trim=False)
        cruda = float(np.atleast_1d(t)[0]) if np.atleast_1d(t).size else 126.0
        # Con silencio librosa devuelve 0 (colgó las 3 réplicas el 30-sep con pistas mudas).
        if not np.isfinite(cruda) or cruda <= 0:
            cruda = 126.0
        semillas, otras = semillas_candidatas(cruda)
    else:
        otras = []

    # búsqueda gruesa ±8 % alrededor de cada semilla (antes ±4 BPM). La semilla de librosa
    # sale cuantizada (a 11025 Hz y hop 512 solo da 99,4 · 107,7 · 117,5 · 129,2 · 143,6…)
    # y con ±4 BPM quedaban tempos imposibles de encontrar: 121,5–125,1 y 133,2–139,5.
    # Un tema de 124 sin etiqueta salía 125,3 (visto en producción como «121.42 … variable»).
    # ±8 % cubre la mitad del salto entre dos valores vecinos hasta ~180 BPM.
    best = (semillas[0], -1e9)
    for semilla in semillas:
        cand = _busqueda_gruesa(env, sr, dur_s, semilla, env3)
        if cand[1] > best[1]:
            best = cand
    for semilla in otras:
        cand = _busqueda_gruesa(env, sr, dur_s, semilla, env3)
        if cand[1] > best[1] * VENTAJA_PROPORCION:
            best = (cand[0], cand[1] / VENTAJA_PROPORCION)

    # refinamiento fino
    coarse = best[0]
    best_f = (coarse, -1e9)
    for bpm in np.arange(coarse - 0.06, coarse + 0.06, 0.004):
        p = 60.0 / bpm
        s = max(_grid_score(env, sr, p, ph, dur_s, env3)
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
