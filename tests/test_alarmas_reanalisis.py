"""#206: un tema reanalizado que sale limpio tiene que borrar sus alarmas viejas.

`worker-result` (repo de la plataforma) reemplaza `analysis_flags` solo si el campo llega en el
resultado; si no llega, no toca la columna. Por eso el worker manda siempre la lista, vacía
cuando la revisión automática no encuentra problemas."""
import os
import sys

import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import soundfile as sf  # noqa: E402
import worker  # noqa: E402
from test_contrato_resultado import SR, tema  # noqa: E402


@pytest.fixture(scope="module")
def ruta(tmp_path_factory):
    p = tmp_path_factory.mktemp("alarmas") / "tema.wav"
    sf.write(p, tema(), SR)
    return str(p)


def test_tema_limpio_manda_la_lista_vacia(ruta, monkeypatch):
    monkeypatch.setattr(worker, "sanity_check", lambda result, dur_ms: ([], 1.0))
    r = worker.analyze(ruta, bpm_seed=124)
    assert r["analysis_flags"] == []
    assert r["analysis_confidence"] == 1.0


def test_tema_con_problemas_manda_sus_alarmas(ruta, monkeypatch):
    monkeypatch.setattr(worker, "sanity_check", lambda result, dur_ms: (["tempo_variable"], 0.8))
    r = worker.analyze(ruta, bpm_seed=124)
    assert r["analysis_flags"] == ["tempo_variable"]
