"""Regresión 2-oct-2026: un tema sin BPM previo salía con el tempo equivocado.

La semilla de librosa sale cuantizada (a 11025 Hz y hop 512 solo da 117,45 o 129,2
cerca de 124) y la búsqueda era de ±4 BPM: 121,5–125,1 y 133,2–139,5 no se podían
encontrar. Un tema de 124 salía 125,3 y uno de 135 salía 125,3. En producción se veía
como «v7 bpm 121.42 → 121.44 (resid 104.9 ms, variable)»."""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from grid_detect import detect_grid, detect_tempo  # noqa: E402

SR = 11025  # la misma frecuencia que usa analyze() en worker.py


def tema(bpm, ancla_s=0.25, dur_s=60, sr=SR):
    """Bombo con clic en cada negra, hat en el contratiempo y un acorde de La menor."""
    t = np.arange(int(sr * dur_s)) / sr
    y = np.zeros_like(t)
    rng = np.random.default_rng(1)
    beat = 60.0 / bpm
    for k in np.arange(ancla_s, dur_s, beat):
        i = int(k * sr)
        n = min(int(0.12 * sr), len(y) - i)
        tt = np.arange(n) / sr
        y[i:i + n] += 0.9 * np.sin(2 * np.pi * (50 + 80 * np.exp(-tt * 30)) * tt) * np.exp(-tt * 18)
        m = min(int(0.01 * sr), len(y) - i)
        y[i:i + m] += 0.5 * rng.standard_normal(m)
        j = int((k + beat / 2) * sr)
        m = min(int(0.02 * sr), max(0, len(y) - j))
        y[j:j + m] += 0.2 * rng.standard_normal(m)
    for f in (220.0, 261.63, 329.63):
        y += 0.08 * np.sin(2 * np.pi * f * t)
    return y.astype(np.float32)


@pytest.mark.parametrize("bpm", [123, 124, 135])
def test_tempo_fuera_del_viejo_rango_de_busqueda(bpm):
    # Antes: 123 → 116,21 · 124 → 125,33 · 135 → 125,34.
    medido, _ = detect_tempo(tema(bpm), SR)
    assert round(medido) == bpm, medido


@pytest.mark.parametrize("bpm", [120, 126, 128])
def test_tempos_que_ya_salian_bien_siguen_igual(bpm):
    medido, _ = detect_tempo(tema(bpm), SR)
    assert round(medido) == bpm, medido


def test_la_semilla_sigue_mandando():
    medido, _ = detect_tempo(tema(124), SR, seed_bpm=124)
    assert round(medido) == 124


def test_ancla_dentro_del_primer_beat():
    bpm, ancla_ms = detect_grid(tema(124), SR)
    assert round(bpm) == 124
    assert 0 <= ancla_ms < 60000 / 124
    assert abs(ancla_ms - 250) <= 15
