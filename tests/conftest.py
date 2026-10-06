"""Las pruebas de process_job usan archivos de mentira (b"x") con analyze simulado: el sondeo
con ffprobe (7.6.15) los rechazaría antes de llegar. Se apaga en todas, salvo en las que llevan
la marca `sondeo_real` (tests/test_archivos_raros.py)."""
import os
import sys

import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def pytest_configure(config):
    config.addinivalue_line("markers", "sondeo_real: usa sondear_audio de verdad (ffprobe)")
    config.addinivalue_line("markers", "aislamiento_real: corre el análisis en un proceso hijo de verdad")


@pytest.fixture(autouse=True)
def _sin_aislamiento(request, monkeypatch):
    """Los dobles de las pruebas guardan lo que pasa en listas del proceso padre: con el análisis
    en un proceso hijo (7.6.17) no se verían. Se apaga salvo con la marca `aislamiento_real`."""
    if request.node.get_closest_marker("aislamiento_real"):
        return
    try:
        import worker
    except Exception:
        return
    monkeypatch.setattr(worker, "AISLAR_ANALISIS", False, raising=False)


@pytest.fixture(autouse=True)
def _sin_sondeo(request, monkeypatch):
    if request.node.get_closest_marker("sondeo_real"):
        return
    try:
        import worker
    except Exception:
        return
    if hasattr(worker, "sondear_audio"):
        monkeypatch.setattr(worker, "sondear_audio", lambda path: None)
