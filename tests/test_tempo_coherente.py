"""Regresión 2-oct-2026: con BPM sembrado, `bpm` y `bpm_fine` no cuadraban.

worker-result guarda round(bpm) + bpm_fine. analyze() mandaba el `bpm` de detect_grid
(que ignora la semilla) y el `bpm_fine` calculado sobre la semilla: con semilla 124 y
detect_grid en 125,26, el tempo efectivo quedaba 125,009 en vez de 124,009."""
import os
import sys

import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import soundfile as sf  # noqa: E402
import worker  # noqa: E402
from test_tempo_sin_semilla import tema  # noqa: E402


@pytest.mark.parametrize("semilla", [124, None])
def test_tempo_efectivo_igual_al_medido(tmp_path, monkeypatch, semilla):
    # detect_grid se equivoca a propósito: el entero tiene que salir de la semilla.
    monkeypatch.setattr(worker, "detect_grid", lambda y, sr, seed_bpm=None: (125.26, 250))
    p = tmp_path / "tema.wav"
    sf.write(p, tema(124, dur_s=64, sr=22050), 22050)
    r = worker.analyze(str(p), bpm_seed=semilla)
    efectivo = round(r["bpm"]) + r["bpm_fine"]
    assert efectivo == pytest.approx(r["bpm_precise"], abs=0.01)
    if semilla:
        assert r["bpm_precise"] == pytest.approx(124, abs=0.05)
