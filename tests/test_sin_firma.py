"""W6 (auditoría P11/P12): los registros y el `error` que llega a la base no llevan URLs firmadas."""
import inspect
import os
import sys
import traceback

import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import grid_verifier  # noqa: E402
import stems_worker  # noqa: E402
import worker  # noqa: E402

MODULOS = [worker, stems_worker, grid_verifier]
JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1cmwiOiJhdWRpby94Lm1wMyJ9.AbC_dEf-123"
URL = f"https://abc.supabase.co/storage/v1/object/sign/audio/x.mp3?token={JWT}&download=1"


@pytest.mark.parametrize("m", MODULOS, ids=lambda m: m.__name__)
def test_tapa_token_y_jwt(m):
    texto = m.sin_firma(f"404 Client Error: Not Found for url: {URL}")
    assert "eyJ" not in texto
    assert "token=***&download=1" in texto
    assert m.sin_firma(f"Authorization: Bearer {JWT}") == "Authorization: Bearer ***"
    s3 = "https://s3/x?X-Amz-Credential=AKIA%2F1&X-Amz-Signature=abc123&X-Amz-Expires=60"
    assert m.sin_firma(s3) == "https://s3/x?X-Amz-Credential=***&X-Amz-Signature=***&X-Amz-Expires=60"


@pytest.mark.parametrize("m", MODULOS, ids=lambda m: m.__name__)
def test_no_toca_lo_demas(m):
    for texto in ("[job 1] OK — cues=8 energy=7", "key=8A bpm=124", "v7 bpm 124.0 → 124.001", ""):
        assert m.sin_firma(texto) == texto
    assert m.sin_firma(None) == "None"


def test_bloque_identico_en_los_tres():
    # Cada imagen copia solo su archivo: el bloque se repite y no debe separarse.
    def fuente(m):
        return [inspect.getsource(o) for o in (m.sin_firma, m._SalidaSinFirmas, m.filtrar_salida)] + [m._FIRMAS.pattern]
    assert fuente(worker) == fuente(stems_worker) == fuente(grid_verifier)


def test_filtrar_salida_tapa_print_y_traceback(capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    stems_worker.filtrar_salida()
    stems_worker.filtrar_salida()  # idempotente: no se envuelve dos veces
    assert isinstance(sys.stdout, stems_worker._SalidaSinFirmas)
    assert not isinstance(sys.stdout._salida, stems_worker._SalidaSinFirmas)
    stems_worker.log("bajando", URL)
    try:
        raise RuntimeError(f"502 Server Error for url: {URL}")
    except RuntimeError:
        traceback.print_exc()
    out, err = capsys.readouterr()
    assert "bajando" in out and "502 Server Error" in err
    assert "eyJ" not in out + err


class Respuesta:
    ok = True
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {}


@pytest.fixture
def enviados(monkeypatch):
    lista = []

    def post(url, **kw):
        lista.append(kw.get("json"))
        return Respuesta()

    monkeypatch.setattr(worker.requests, "post", post)
    monkeypatch.setattr(stems_worker.requests, "post", post)
    return lista


def test_error_a_la_base_sin_firma(enviados):
    error = f"404 Client Error: Not Found for url: {URL}"
    worker.send_result("j", "t", "error", error=error)
    stems_worker.report("j", False, error=error)
    stems_worker.report_loops("j", False, error=error)
    stems_worker.report_render("j", False, error=error)
    assert len(enviados) == 4
    for body in enviados:
        assert "token=***" in body["error"] and "eyJ" not in body["error"]


def test_codigo_error_del_verificador_sin_firma():
    assert "eyJ" not in grid_verifier.codigo_error(RuntimeError(f"HTTP Error 403 for {URL}"))
    assert grid_verifier.codigo_error(RuntimeError("no_anchor")) == "determinista:no_anchor"
