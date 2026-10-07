"""Parecido v0 entre una toma generada y su tema semilla (6-oct-2026, pedido del Estudio).

Sin modelo de embeddings: compara una «huella de rasgos» liviana de cada tema.

    bruto 0–100 = 15·BPM + 15·tonalidad + 25·energía + 45·timbre

- BPM: max(0, 1 − |Δ|/6), contra el tempo de la semilla, su mitad y su doble (el mejor).
- Tonalidad (Camelot): misma 1; relativa o a un paso en la rueda 0,8; a dos pasos 0,4; si no, 0.
- Energía: correlación de Pearson entre las curvas de RMS por compás (32 puntos), (r+1)/2.
- Timbre: por bloque (media y desvío de MFCC 1–19, contraste espectral, tercios de octava),
  coseno entre los vectores centrados en su propia media, (c+1)/2; luego un promedio con pesos.
  El MFCC 0 queda fuera: mide el volumen y dominaría el coseno. Cada bloque se compara por
  separado para que ninguno pese más solo por su escala.

Si a la REFERENCIA le falta un componente (semilla sin BPM o sin tonalidad), su peso se reparte
entre los demás y queda anotado en `faltan`. Si la referencia lo tiene y la toma no (una toma
sin pulso contra una semilla de club), eso es diferencia: BPM y tonalidad valen 0 y la energía
0,5 (correlación nula).

Línea base: el mismo bruto entre la toma y unos temas al azar del mismo DJ.
    parecido = clamp(100·(S_semilla − media_base)/(100 − media_base), 0, 100)

Funciones puras: `huella` recibe el audio ya cargado (no lee archivos ni red)."""
import math

import numpy as np

VERSION = 1
PESOS = {"bpm": 15.0, "tonalidad": 15.0, "energia": 25.0, "timbre": 45.0}
PESOS_TIMBRE = {"mfcc_media": 0.35, "mfcc_desvio": 0.15, "contraste": 0.2, "tercios": 0.3}
PUNTOS_ENERGIA = 32
N_MFCC = 20           # se guardan los coeficientes 1–19 (el 0 es el volumen)
BANDAS_CONTRASTE = 4  # a 11025 Hz: 200·2^5 = 6400 Hz supera Nyquist con más bandas
VENTANA_SIN_REJILLA_S = 2.0


def _r(x, n=4):
    return [round(float(v), n) for v in x]


def curva_energia(y: np.ndarray, sr: int, bpm=None, ancla_ms=None, puntos: int = PUNTOS_ENERGIA):
    """RMS por compás con la rejilla del tema (o ventanas de 2 s sin BPM), remuestreado a
    `puntos` valores. None si el tema es demasiado corto para tener forma."""
    if bpm and 40 < float(bpm) < 240:
        paso = round(sr * 4 * 60.0 / float(bpm))
        inicio = round(sr * float(ancla_ms or 0) / 1000.0) % max(paso, 1)
    else:
        paso, inicio = int(sr * VENTANA_SIN_REJILLA_S), 0
    n = (len(y) - inicio) // paso if paso > 0 else 0
    if n < 4:
        return None
    bloques = y[inicio:inicio + n * paso].reshape(n, paso)
    rms = np.sqrt(np.mean(bloques.astype(np.float64) ** 2, axis=1))
    x = np.linspace(0, n - 1, puntos)
    return _r(np.interp(x, np.arange(n), rms), 6)


def huella(y: np.ndarray, sr: int, bpm=None, ancla_ms=None, camelot=None) -> dict:
    """Rasgos del tema para compararlo después sin volver a bajar el audio."""
    import librosa

    import analizador_v8 as v8
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=N_MFCC)[1:]
    contraste = librosa.feature.spectral_contrast(y=y, sr=sr, n_bands=BANDAS_CONTRASTE)
    return {
        "v": VERSION,
        "sr": sr,
        "bpm": round(float(bpm), 3) if bpm else None,
        "tonalidad": camelot,
        "mfcc_media": _r(mfcc.mean(axis=1)),
        "mfcc_desvio": _r(mfcc.std(axis=1)),
        "contraste": _r(contraste.mean(axis=1)),
        "tercios": v8.tercios_de_octava(y, sr),
        "energia": curva_energia(y, sr, bpm, ancla_ms),
    }


# ── componentes ──────────────────────────────────────────────────────────────

def parecido_bpm(bpm_toma, bpm_semilla):
    if not bpm_toma or not bpm_semilla:
        return None
    return max(max(0.0, 1.0 - abs(float(bpm_toma) - float(bpm_semilla) * f) / 6.0) for f in (1.0, 0.5, 2.0))


def _camelot(c):
    try:
        c = str(c).strip().upper()
        n, letra = int(c[:-1]), c[-1]
        return (n, letra) if 1 <= n <= 12 and letra in "AB" else None
    except (ValueError, IndexError):
        return None


def parecido_tonalidad(a, b):
    ca, cb = _camelot(a), _camelot(b)
    if not ca or not cb:
        return None
    paso = min((ca[0] - cb[0]) % 12, (cb[0] - ca[0]) % 12)
    if ca[1] == cb[1]:
        return {0: 1.0, 1: 0.8, 2: 0.4}.get(paso, 0.0)
    return 0.8 if paso == 0 else 0.0  # relativa: mismo número, otra letra


def parecido_energia(a, b):
    if not a or not b or len(a) != len(b):
        return None
    a, b = np.asarray(a, float), np.asarray(b, float)
    if a.std() < 1e-12 or b.std() < 1e-12:
        return None
    r = float(np.corrcoef(a, b)[0, 1])
    return (r + 1.0) / 2.0 if np.isfinite(r) else None


def _coseno_centrado(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    if a.size == 0 or a.size != b.size:
        return None
    a, b = a - a.mean(), b - b.mean()
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-12 or nb < 1e-12:
        return None
    return (float(np.dot(a, b) / (na * nb)) + 1.0) / 2.0


def parecido_timbre(ha: dict, hb: dict):
    """Promedio con pesos de los bloques; devuelve (valor, detalle por bloque)."""
    detalle = {}
    for k in ("mfcc_media", "mfcc_desvio", "contraste"):
        detalle[k] = _coseno_centrado(ha.get(k) or [], hb.get(k) or [])
    ta, tb = ha.get("tercios") or {}, hb.get("tercios") or {}
    comunes = [k for k in ta if k in tb]
    detalle["tercios"] = _coseno_centrado([ta[k] for k in comunes], [tb[k] for k in comunes]) if len(comunes) >= 3 else None
    presentes = {k: v for k, v in detalle.items() if v is not None}
    if not presentes:
        return None, detalle
    peso = sum(PESOS_TIMBRE[k] for k in presentes)
    return sum(PESOS_TIMBRE[k] * v for k, v in presentes.items()) / peso, detalle


def puntaje_bruto(ha: dict, hb: dict):
    """(bruto 0–100 o None, componentes). `hb` es la referencia (la semilla o un tema base)."""
    timbre, detalle = parecido_timbre(ha, hb)
    comp = {
        "bpm": parecido_bpm(ha.get("bpm"), hb.get("bpm")),
        "tonalidad": parecido_tonalidad(ha.get("tonalidad"), hb.get("tonalidad")),
        "energia": parecido_energia(ha.get("energia"), hb.get("energia")),
        "timbre": timbre,
    }
    # La referencia tiene el dato y la toma no: cuenta como diferencia, no como hueco.
    if comp["bpm"] is None and hb.get("bpm"):
        comp["bpm"] = 0.0
    if comp["tonalidad"] is None and _camelot(hb.get("tonalidad")):
        comp["tonalidad"] = 0.0
    eb = hb.get("energia")
    if comp["energia"] is None and eb and np.std(np.asarray(eb, float)) >= 1e-12:
        comp["energia"] = 0.5
    presentes = {k: v for k, v in comp.items() if v is not None}
    salida = {k: (round(v, 4) if v is not None else None) for k, v in comp.items()}
    salida["timbre_bloques"] = {k: (round(v, 4) if v is not None else None) for k, v in detalle.items()}
    if "timbre" not in presentes:
        return None, salida  # sin timbre no hay comparación que valga
    peso = sum(PESOS[k] for k in presentes)
    salida["faltan"] = sorted(set(comp) - set(presentes))
    return round(100.0 * sum(PESOS[k] * v for k, v in presentes.items()) / peso, 2), salida


def medir_parecido(h_toma: dict, h_semilla: dict, bases: list) -> dict:
    """`bases`: lista de (track_id, huella). Devuelve lo que se guarda en parecido_tomas."""
    bruto, comp = puntaje_bruto(h_toma, h_semilla)
    brutos = []
    for tid, hb in bases or []:
        b, _ = puntaje_bruto(h_toma, hb)
        if b is not None:
            brutos.append({"track_id": tid, "bruto": b})
    media = round(sum(x["bruto"] for x in brutos) / len(brutos), 2) if brutos else None
    puntaje = None
    if bruto is not None and media is not None and media < 100.0:
        puntaje = round(min(100.0, max(0.0, 100.0 * (bruto - media) / (100.0 - media))), 1)
    return {"version": VERSION, "puntaje": puntaje, "bruto": bruto, "componentes": comp,
            "linea_base": {"temas": brutos, "media": media}}


def huella_valida(h) -> bool:
    return isinstance(h, dict) and h.get("v") == VERSION and bool(h.get("mfcc_media"))


def huella_de_archivo(path: str, bpm=None, ancla_ms=None, camelot=None, sr: int = 11025, duracion=600) -> dict:
    """Para la semilla sin huella guardada o una toma de calibración: carga y mide."""
    import librosa
    y, sr = librosa.load(path, sr=sr, mono=True, duration=duracion)
    if y.size == 0 or not math.isfinite(float(np.max(np.abs(y)))):
        raise ValueError("audio vacío")
    return huella(y, sr, bpm, ancla_ms, camelot)
