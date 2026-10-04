"""#206 (4-oct): a 136 BPM sin etiqueta, con hi-hat a contratiempo, la semilla ×2/3 (90,7)
le ganaba al tempo real. Su rejilla de 1,5 tiempos = 3 medios tiempos cae siempre sobre un
golpe. Regla: una proporción explicada por la rejilla de medio tiempo de la octava no compite."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import grid_detect as gd  # noqa: E402

SR = 22050


def con_contratiempo(bpm, dur_s=30, ancla_s=0.25):
    """Bombo en cada tiempo y hi-hat a contratiempo (el caso que falla)."""
    t = np.arange(int(SR * dur_s)) / SR
    y = np.zeros_like(t)
    rng = np.random.default_rng(7)
    beat = 60.0 / bpm
    for k in np.arange(ancla_s, dur_s, beat):
        i = int(k * SR)
        n = min(int(0.12 * SR), len(y) - i)
        tt = np.arange(n) / SR
        y[i:i + n] += 0.9 * np.sin(2 * np.pi * (50 + 80 * np.exp(-tt * 30)) * tt) * np.exp(-tt * 18)
        j = int((k + beat / 2) * SR)
        m = min(int(0.02 * SR), max(0, len(y) - j))
        y[j:j + m] += 0.2 * rng.standard_normal(m)
    return y.astype(np.float32)


def test_dos_tercios_explicada_por_la_media_rejilla():
    assert gd.explicada_por_media_rejilla(90.54, 136.0)   # el ❌ de #206: 3,004 medios tiempos
    assert gd.explicada_por_media_rejilla(136 * 2 / 3, 136.0)
    assert gd.explicada_por_media_rejilla(92.0, 138.0)


def test_las_proporciones_buenas_siguen_compitiendo():
    # «Right Thing» (2-oct): la octava de 80,75 se iba a 174,33; ×1,5 da 121 → 123, que es el bueno.
    assert not gd.explicada_por_media_rejilla(123.0, 174.33)
    assert not gd.explicada_por_media_rejilla(123.0, 161.5)
    # ×4/3 y ×0,75 de la octava: 1,5 y 2,667 medios tiempos.
    assert not gd.explicada_por_media_rejilla(124.0 * 4 / 3, 124.0)
    assert not gd.explicada_por_media_rejilla(124.0 * 0.75, 124.0)
    # Datos raros: nunca explica.
    assert not gd.explicada_por_media_rejilla(0, 124.0)
    assert not gd.explicada_por_media_rejilla(None, 124.0)


def test_136_con_contratiempo_sin_semilla_da_136():
    """Falla sin el arreglo: detect_tempo devolvía ~90,5 (2/3 del tempo)."""
    bpm, _ = gd.detect_tempo(con_contratiempo(136), SR, seed_bpm=None)
    assert round(bpm) == 136, bpm
