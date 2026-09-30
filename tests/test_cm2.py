"""W3 (30-sep-2026): CM2 pedia stream-track sin x-worker-secret (401/404 en cada tema)."""
import os
import sys

import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import worker  # noqa: E402


class Respuesta:
    def __init__(self, tipo="audio/mp4", status_code=200):
        self.headers = {"content-type": tipo}
        self.content = b"audio"
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise worker.requests.HTTPError(str(self.status_code))


def test_descarga_manda_el_secreto(monkeypatch):
    pedidos = []

    def get(url, headers=None, timeout=None):
        pedidos.append((url, headers))
        return Respuesta()

    monkeypatch.setattr(worker.requests, "get", get)
    ruta = worker.descargar_rendicion("abc")
    try:
        url, headers = pedidos[0]
        assert url.endswith("/stream-track?track_id=abc&format=aac")
        assert headers["x-worker-secret"] == worker.WORKER_SECRET
        assert ruta.endswith(".m4a")
    finally:
        os.remove(ruta)


def test_extension_segun_content_type(monkeypatch):
    monkeypatch.setattr(worker.requests, "get", lambda *a, **k: Respuesta("audio/mpeg"))
    ruta = worker.descargar_rendicion("abc")
    os.remove(ruta)
    assert ruta.endswith(".mp3")


def test_si_falta_el_m4a_pide_el_mp3(monkeypatch):
    pedidos = []

    def get(url, headers=None, timeout=None):
        pedidos.append(url)
        return Respuesta("application/json", 502) if url.endswith("aac") else Respuesta("audio/mpeg")

    monkeypatch.setattr(worker.requests, "get", get)
    ruta = worker.descargar_rendicion("abc")
    os.remove(ruta)
    assert [u.rsplit("=", 1)[1] for u in pedidos] == ["aac", "mp3"]
    assert ruta.endswith(".mp3")


def test_no_autorizado_no_reintenta(monkeypatch):
    pedidos = []
    monkeypatch.setattr(worker.requests, "get",
                        lambda url, **k: pedidos.append(url) or Respuesta("application/json", 401))
    with pytest.raises(worker.requests.HTTPError):
        worker.descargar_rendicion("abc")
    assert len(pedidos) == 1


def test_temporal_se_borra_si_el_ancla_falla(monkeypatch):
    rutas = []
    monkeypatch.setattr(worker.requests, "get", lambda *a, **k: Respuesta())

    def falla(ruta, bpm):
        rutas.append(ruta)
        raise RuntimeError("CM2: audio vacío")

    monkeypatch.setattr(worker, "compute_anchor", falla)
    with pytest.raises(RuntimeError):
        worker.ancla_de_rendicion("abc", 124.0)
    assert not os.path.exists(rutas[0])


@pytest.mark.parametrize("variable,examen,esperado", [
    (True, True, True),
    (True, False, False),   # hoy en produccion: variable puesta, examen NO APROBADO
    (False, True, False),
])
def test_cm2_exige_examen_aprobado(monkeypatch, variable, examen, esperado):
    monkeypatch.setattr(worker, "ENABLE_ANCHOR_BACKFILL", variable)
    monkeypatch.setattr(worker, "EXAMEN_CM2_APROBADO", examen)
    assert worker.cm2_habilitado() is esperado
