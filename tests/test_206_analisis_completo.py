"""#206 de punta a punta: el camino completo del análisis (`analyze()`, el mismo que corre
en Railway) sobre sintéticos SIN etiqueta de BPM, a tempos dentro de los dos rangos del
bug (121,5–125,1 y 133,2–139,5). El lote real de Germán (4-oct) trajo todo con BPM, así
que esta es la prueba que lo cierra. Deja la tabla esperado / medido en el resumen de la
acción de GitHub (`GITHUB_STEP_SUMMARY`)."""
import os
import sys
import warnings

import numpy as np
import pytest
import soundfile as sf

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import worker  # noqa: E402

SR = 44100
FILAS = []


def sintetico(bpm, dur_s=40, ancla_s=0.25):
    """El de #108/#573: bombo en cada tiempo, hi-hat a contratiempo y acorde de La menor."""
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
    for f in (220.0, 261.63, 329.63):
        y += 0.08 * np.sin(2 * np.pi * f * t)
    return np.stack([y, y], axis=1).astype(np.float32)


@pytest.mark.parametrize("bpm", [122, 123, 124, 125, 134, 136, 138])
def test_analisis_completo_sin_bpm_previo(bpm, tmp_path):
    ruta = tmp_path / f"sin_etiqueta_{bpm}.wav"
    sf.write(ruta, sintetico(bpm), SR)              # WAV sin etiquetas: nada de BPM previo
    r = worker.analyze(str(ruta), bpm_seed=None)
    FILAS.append((bpm, r.get("bpm"), r.get("bpm_precise"), r.get("tempo_stability")))
    warnings.warn(f"#206 esperado={bpm} bpm={r.get('bpm')} bpm_precise={r.get('bpm_precise')} "
                  f"tempo={r.get('tempo_stability')}")  # se ve en el log de la acción aunque pase
    assert round(float(r["bpm"])) == bpm, r.get("bpm")
    assert r.get("tempo_stability") == "constante"


def teardown_module(_):
    destino = os.environ.get("GITHUB_STEP_SUMMARY")
    if not destino or not FILAS:
        return
    with open(destino, "a", encoding="utf-8") as f:
        f.write("### #206 · análisis completo sin BPM previo\n\n| esperado | bpm | bpm_precise | tempo |\n|---|---|---|---|\n")
        for fila in sorted(FILAS):
            f.write("| {} | {} | {} | {} |\n".format(*fila))
