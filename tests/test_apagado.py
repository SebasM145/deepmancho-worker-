"""SIGTERM (3-oct-2026): un redespliegue cortaba las réplicas a mitad de un trabajo y el tema
quedaba en `processing` 8 min. Ahora el trabajo en curso vuelve a la cola al instante."""
import os
import signal
import sys

import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import worker  # noqa: E402


@pytest.fixture(autouse=True)
def estado_limpio(monkeypatch):
    monkeypatch.setattr(worker, "APAGANDO", False)
    monkeypatch.setattr(worker, "EN_TRABAJO", False)
    viejo = signal.signal(signal.SIGTERM, worker._al_apagar)
    yield
    signal.signal(signal.SIGTERM, viejo)


def test_sigterm_en_medio_de_un_trabajo_lo_devuelve_a_la_cola(monkeypatch, tmp_path):
    enviados = []
    p = tmp_path / "a.wav"
    p.write_bytes(b"x")
    monkeypatch.setattr(worker, "download_audio", lambda url: str(p))

    def analizando(path, bpm_seed=None):
        os.kill(os.getpid(), signal.SIGTERM)   # Railway redespliega justo ahora
        return {"energy": 7}
    monkeypatch.setattr(worker, "analyze", analizando)
    monkeypatch.setattr(worker, "send_result", lambda *a, **k: enviados.append((a, k)))
    worker.process_job({"id": "j", "track_id": "t"}, {}, "http://x/a.wav")
    (args, kw), = enviados
    assert args[2] == "error" and "SIGTERM" in kw["error"]   # worker-result lo pasa a pending
    assert worker.APAGANDO is True and worker.EN_TRABAJO is False
    assert not p.exists()                                    # el temporal se borra igual


def test_sigterm_sin_trabajo_sale_limpio():
    with pytest.raises(SystemExit) as e:
        os.kill(os.getpid(), signal.SIGTERM)
        signal.pause() if hasattr(signal, "pause") else None
    assert e.value.code == 0
    assert worker.APAGANDO is True


def test_tras_sigterm_no_pide_mas_temas(monkeypatch):
    pedidos = []
    monkeypatch.setattr(worker, "filtrar_salida", lambda: None)
    monkeypatch.setattr(worker, "GOLDEN_EXAM", False)
    monkeypatch.setattr(worker, "next_job", lambda: pedidos.append(1) or (None, None, None, None))
    monkeypatch.setattr(worker, "APAGANDO", True)
    with pytest.raises(SystemExit):
        worker.main()
    assert pedidos == []
