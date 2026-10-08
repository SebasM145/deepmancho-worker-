"""#206, los 2 raros del 7-oct: temas sin etiqueta guardados a 160,007 y 162,502 («Minimal /
Deep Tech»), sospechosos de ×4/3 de 120 y 122. En minimal es común una percusión que repite
cada 3 semicorcheas (0,75 tiempos): su rejilla es la de 4/3 del tempo. La envolvente de ataques
le da más peso a ese ruido de banda ancha que al bombo, y la rejilla de 160 puntuaba más que
la de 120 aunque el bombo pegara en cada tiempo de 120. Medido antes del arreglo (7.6.18):
120 → 160,0 · 122 → 162,68 · 126 → 168,0, todos «constante»; sin esa percusión, exactos.

El arreglo extiende el desempate por el bombo de #618 (que solo miraba parejas 3:2) a las
parejas 4:3. Un tema de 160 de verdad, con bombo en cada tiempo de 160, se queda en 160."""
import os
import sys

import numpy as np
import pytest
import soundfile as sf

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import grid_detect as gd  # noqa: E402
import worker  # noqa: E402

SR = 44100


def _golpe(y, t_s, n_s, senal):
    i = int(t_s * SR)
    n = min(int(n_s * SR), len(y) - i)
    if n > 0:
        y[i:i + n] += senal(np.arange(n) / SR)


def minimal(bpm, dur_s=60, ancla_s=0.25, percusion=0.3, compases_con_bombo=8, compases_sin_bombo=0):
    """Bombo en cada tiempo de `bpm` (con pausas opcionales de `compases_sin_bombo`), percusión
    de ruido cada 3 semicorcheas y un acorde de La menor de fondo."""
    y = np.zeros(int(SR * dur_s))
    rng = np.random.default_rng(3)
    beat = 60.0 / bpm
    ciclo = compases_con_bombo + compases_sin_bombo
    for i, k in enumerate(np.arange(ancla_s, dur_s, beat)):
        if (i // 4) % ciclo < compases_con_bombo:
            _golpe(y, k, 0.12, lambda tt: 0.9 * np.sin(2 * np.pi * (50 + 80 * np.exp(-tt * 30)) * tt) * np.exp(-tt * 18))
    if percusion:
        for k in np.arange(ancla_s, dur_s, 0.75 * beat):
            _golpe(y, k, 0.05, lambda tt: percusion * rng.standard_normal(len(tt)) * np.exp(-tt * 60))
    t = np.arange(len(y)) / SR
    for f in (220.0, 261.63, 329.63):
        y += 0.05 * np.sin(2 * np.pi * f * t)
    return np.stack([y, y], axis=1).astype(np.float32)


def test_proporcion_cuatro_tercios():
    assert gd.en_proporcion_4_3(160.0, 120.0)
    assert gd.en_proporcion_4_3(120.0, 160.0)
    assert gd.en_proporcion_4_3(162.68, 121.98)
    assert not gd.en_proporcion_4_3(180.0, 120.0)      # 3:2 lo cubre la regla de #206/#618
    assert not gd.en_proporcion_4_3(240.0, 120.0)
    assert not gd.en_proporcion_4_3(124.0, 124.0)
    assert not gd.en_proporcion_4_3(165.0, 120.0)      # 1,375: ni 4:3 ni 3:2
    assert not gd.en_proporcion_4_3(None, 120.0)


@pytest.mark.parametrize("bpm", [120, 122, 126])
def test_percusion_de_tres_semicorcheas_no_lleva_a_cuatro_tercios(bpm, tmp_path):
    ruta = tmp_path / f"minimal_{bpm}.wav"
    sf.write(ruta, minimal(bpm), SR)
    r = worker.analyze(str(ruta), bpm_seed=None)
    assert round(float(r["bpm"])) == bpm, r.get("bpm")
    assert r.get("tempo_stability") == "constante"


def test_con_pausas_largas_de_bombo(tmp_path):
    """El caso real `02589893`: 109 bombos/min en un tema que sería de 120 (con pausas)."""
    ruta = tmp_path / "minimal_120_pausas.wav"
    sf.write(ruta, minimal(120, dur_s=90, compases_con_bombo=8, compases_sin_bombo=4), SR)
    r = worker.analyze(str(ruta), bpm_seed=None)
    assert round(float(r["bpm"])) == 120, r.get("bpm")


def test_un_tema_de_160_de_verdad_sigue_en_160(tmp_path):
    """Control: con el bombo en cada tiempo de 160, la misma percusión no lo baja a 120."""
    ruta = tmp_path / "rapido_160.wav"
    sf.write(ruta, minimal(160, percusion=0.3), SR)
    r = worker.analyze(str(ruta), bpm_seed=None)
    assert round(float(r["bpm"])) == 160, r.get("bpm")
