"""W6 (3-oct-2026): los errores que van a la base desde la limpieza de copias de escucha
(#265) y desde el render de sets (#143) no llevan el token de la URL firmada.

Con un error de conexión, requests no escribe la URL entera sino
«Max retries exceeded with url: /storage/v1/object/sign/…?token=eyJ…»: sin
`https://`, `_sin_firmas` no la reconocía. Y la limpieza mandaba `str(e)` tal cual."""
import os
import sys

import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import worker  # noqa: E402

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJ1cmwiOiJtdXNpYy9hLm1wMyJ9.c2VjcmV0b2RlZmlybWE"
FIRMADA = f"https://x.supabase.co/storage/v1/object/sign/music/a.mp3?token={JWT}"
# Lo que dice requests cuando no llega a conectar (medido con requests 2.32).
SIN_CONEXION = ("HTTPSConnectionPool(host='x.supabase.co', port=443): Max retries exceeded with url: "
                f"/storage/v1/object/sign/music/a.mp3?token={JWT} (Caused by NewConnectionError(...))")


def test_sin_firmas_sola_no_tapaba_la_ruta_sin_host():
    """El hueco que motivó el arreglo: documenta por qué hace falta sin_firma encima."""
    assert JWT in worker._sin_firmas(SIN_CONEXION)
    assert JWT not in worker.sin_firma(worker._sin_firmas(SIN_CONEXION))


class R:
    def __init__(self, status=200, js=None):
        self.status_code, self._js = status, js or {}

    def json(self):
        return self._js

    def raise_for_status(self):
        pass


@pytest.mark.parametrize("error", [SIN_CONEXION, f"404 Client Error: Not Found for url: {FIRMADA}"])
def test_limpieza_de_copias_reporta_sin_token(monkeypatch, capsys, error):
    llamadas = []

    def post(url, headers=None, json=None, timeout=None):
        accion = url.split("action=")[1]
        llamadas.append((accion, json))
        if accion == "next":
            return R(200, {"job": {"id": "j1", "track_id": "t1"}, "audio_url": FIRMADA,
                           "upload": {"url": FIRMADA, "path": "dj/streams/x.mp3"}})
        return R(200, {"ok": True})

    def get(url, timeout=None):
        raise worker.requests.ConnectionError(error)

    monkeypatch.setattr(worker, "LIMPIEZA_DISPONIBLE", True)
    monkeypatch.setattr(worker.requests, "post", post)
    monkeypatch.setattr(worker.requests, "get", get)
    assert worker.poll_limpiar_streams() is True
    accion, cuerpo = llamadas[-1]
    assert accion == "fail"
    assert JWT not in cuerpo["error"] and "eyJ" not in cuerpo["error"]
    assert "a.mp3" in cuerpo["error"]  # se sigue sabiendo qué archivo falló
    assert JWT not in capsys.readouterr().out


@pytest.mark.parametrize("error", [SIN_CONEXION, f"404 Client Error: Not Found for url: {FIRMADA}"])
def test_render_de_set_reporta_sin_token(monkeypatch, error):
    llamadas = []

    def set_api(action, payload=None):
        llamadas.append((action, payload))
        if action == "next":
            return {"job": {"id": "s1", "title": "Set"}, "tracks": [], "upload_url": FIRMADA,
                    "result_path": "sets/s1.mp3"}
        return {"ok": True}

    def render_set(*a, **k):
        raise worker.requests.ConnectionError(error)

    monkeypatch.setattr(worker, "_set_api", set_api)
    monkeypatch.setattr(worker, "render_set", render_set)
    monkeypatch.setattr(worker.traceback, "print_exc", lambda: None)
    assert worker.poll_set_render() is True
    accion, cuerpo = llamadas[-1]
    assert accion == "fail"
    assert JWT not in cuerpo["error"] and "eyJ" not in cuerpo["error"]
    assert "a.mp3" in cuerpo["error"]
