"""Analizador v8 (E2 de docs/metodo/analizador-y-semillas.md).

Se arma por fases. Cada fase es una funcion pura que recibe audio y devuelve un bloque
JSON; el servicio y la cola llegan cuando la plataforma tenga donde guardarlo.
Nunca escribe en la base.

F1 · mezcla: sonoridad estereo (BS.1770), true peak x4, LRA (EBU Tech 3342), crest,
     ancho estereo (M/S) y nivel por tercio de octava. Ademas, `energia_v2`, el
     candidato para arreglar #248 (la energia 1-10 actual se satura).
"""
from __future__ import annotations

import math

import numpy as np

VERSION = "v8-F1"

# Tercios de octava ISO 266 entre 31,5 Hz y 16 kHz.
TERCIOS_HZ = (31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1000,
              1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500, 16000)


def _r(x, n=2):
    return None if x is None or not np.isfinite(x) else round(float(x), n)


def _estereo(audio: np.ndarray) -> np.ndarray:
    """(n,) o (n, c) -> (n, 2) float64."""
    a = np.asarray(audio, dtype=np.float64)
    if a.ndim == 1:
        a = np.stack([a, a], axis=1)
    if a.shape[1] == 1:
        a = np.repeat(a, 2, axis=1)
    return a[:, :2]


def true_peak_db(audio: np.ndarray, sr: int) -> float:
    """Pico real (dBTP) con sobremuestreo x4 (BS.1770-4, anexo 2)."""
    from scipy.signal import resample_poly
    a = _estereo(audio)
    sobre = resample_poly(a, 4, 1, axis=0) if sr < 176400 else a
    pico = float(np.max(np.abs(sobre))) if sobre.size else 0.0
    return 20 * math.log10(pico) if pico > 0 else -math.inf


def _sonoridad_bloques(a: np.ndarray, sr: int, largo_s: float, paso_s: float) -> np.ndarray:
    """Sonoridad (LUFS) de bloques K-ponderados de `largo_s` cada `paso_s`."""
    import pyloudnorm as pyln
    medidor = pyln.Meter(sr)
    k = a.copy()
    for filtro in medidor._filters.values():  # el mismo filtro K de pyloudnorm, canal por canal
        for ch in range(k.shape[1]):
            k[:, ch] = filtro.apply_filter(k[:, ch])
    n, p = int(largo_s * sr), int(paso_s * sr)
    if len(k) < n:
        return np.array([])
    cuad = np.cumsum(np.concatenate([np.zeros((1, 2)), k ** 2]), axis=0)
    ini = np.arange(0, len(k) - n + 1, p)
    z = (cuad[ini + n] - cuad[ini]) / n          # potencia media por canal
    pot = z.sum(axis=1)                          # L + R con peso 1 (BS.1770)
    with np.errstate(divide="ignore"):
        return -0.691 + 10 * np.log10(pot)


def lra_lu(audio: np.ndarray, sr: int) -> float | None:
    """Rango de sonoridad (EBU Tech 3342): corto plazo de 3 s cada 1 s, puerta absoluta
    de -70 LUFS y relativa de -20 LU, percentiles 10 y 95."""
    st = _sonoridad_bloques(_estereo(audio), sr, 3.0, 1.0)
    st = st[np.isfinite(st) & (st > -70)]
    if st.size < 2:
        return None
    media = 10 * np.log10(np.mean(10 ** (st / 10)))
    st = st[st > media - 20]
    if st.size < 2:
        return None
    return float(np.percentile(st, 95) - np.percentile(st, 10))


def medidas_mezcla(audio: np.ndarray, sr: int) -> dict:
    """Bloque `mezcla` del v8 (F1)."""
    import pyloudnorm as pyln
    a = _estereo(audio)
    lufs = pyln.Meter(sr).integrated_loudness(a)
    rms = float(np.sqrt(np.mean(a ** 2))) if a.size else 0.0
    pico = float(np.max(np.abs(a))) if a.size else 0.0
    medio, lado = (a[:, 0] + a[:, 1]) / 2, (a[:, 0] - a[:, 1]) / 2
    e_m, e_s = float(np.sum(medio ** 2)), float(np.sum(lado ** 2))
    corr = float(np.corrcoef(a[:, 0], a[:, 1])[0, 1]) if a.size and np.std(a[:, 0]) > 0 and np.std(a[:, 1]) > 0 else 1.0
    return {
        "lufs_integrado": _r(lufs, 1),
        "true_peak_dbtp": _r(true_peak_db(a, sr), 2),
        "lra_lu": _r(lra_lu(a, sr), 1),
        "crest_db": _r(20 * math.log10(pico / rms) if rms > 0 and pico > 0 else None, 1),
        "ancho_estereo": _r(e_s / (e_m + e_s) if e_m + e_s > 0 else 0.0, 3),  # 0 = mono, 0,5 = lados iguales
        "correlacion_lr": _r(corr, 3),
        "tercios_db": tercios_de_octava(medio, sr),
    }


def tercios_de_octava(mono: np.ndarray, sr: int) -> dict:
    """Nivel relativo (dB, el tercio mas fuerte = 0) por tercio de octava, con Welch."""
    from scipy.signal import welch
    if len(mono) < sr:
        return {}
    f, p = welch(mono, fs=sr, nperseg=8192)
    out = {}
    for fc in TERCIOS_HZ:
        lo, hi = fc / 2 ** (1 / 6), fc * 2 ** (1 / 6)
        if hi > sr / 2:
            break
        m = (f >= lo) & (f < hi)
        out[str(fc)] = float(np.sum(p[m])) if m.any() else 0.0
    tope = max(out.values()) if out else 0.0
    return {k: _r(10 * math.log10(v / tope) if v > 0 and tope > 0 else -120.0, 1) for k, v in out.items()}


# #248 (9-oct): escalas al rango REAL del catálogo. Medido en 40 temas al azar del dueño (p10–p90):
# LUFS −10,7…−7,3 · agudos 0,035…0,10 · golpes 4,6…7,1/s. Con las de antes (−20…−6 LUFS, agudos/0,25,
# golpes 1…6/s) el componente de golpes quedaba topado en el 52 % de los temas, los agudos usaban solo
# 0,14–0,41 y el 70 % salía en 7. Con estas: 6 valores y el más común con el 30 %, mismo orden (Spearman 0,96).
V2_LUFS = (-13.0, -6.0)
V2_AGUDOS = (0.02, 0.12)
V2_GOLPES = (3.0, 8.0)


def _escala(x: float, rango: tuple) -> float:
    lo, hi = rango
    return min(1.0, max(0.0, (x - lo) / (hi - lo)))


def energia_v2(mezcla: dict, onsets_por_seg: float | None = None) -> int | None:
    """Candidato para #248, aun SIN calibrar con el oído del dueño: no reemplaza a `energy`
    hasta tener su referencia (H-5). Sonoridad, agudos absolutos (tercios desde 2 kHz contra
    el total, sin normalizar por tema) y densidad de golpes, cada uno en la escala del
    catálogo real (V2_*)."""
    lufs = mezcla.get("lufs_integrado")
    if lufs is None:
        return None
    vol = _escala(lufs, V2_LUFS)
    t = mezcla.get("tercios_db") or {}
    lin = {float(k): 10 ** (v / 10) for k, v in t.items() if v is not None}
    total = sum(lin.values()) or 1.0
    agudos = _escala(sum(v for k, v in lin.items() if k >= 2000) / total, V2_AGUDOS)
    golpes = _escala(onsets_por_seg or 2.0, V2_GOLPES)
    e01 = 0.5 * vol + 0.25 * agudos + 0.25 * golpes
    return int(max(1, min(10, round(1 + 9 * e01))))
