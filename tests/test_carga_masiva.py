"""Carga masiva (2-oct, ~1.000 temas con 5 réplicas): tope de descarga, tope por trabajo
por debajo del plazo de reclamo y `no_anchor` como carrera, no como falla definitiva."""
import os
import sys
import time

import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import grid_verifier as gv  # noqa: E402
import worker  # noqa: E402

PLAZO_CLAIM_S = 8 * 60  # claim_analysis_job retoma 'processing' de más de 8 min


class Descarga:
    def __init__(self, partes, largo=None):
        self.partes, self.headers = partes, ({"content-length": str(largo)} if largo else {})

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=None):
        yield from self.partes


def test_tope_por_trabajo_menor_que_el_plazo_del_claim():
    assert worker.TOPE_TRABAJO_S < PLAZO_CLAIM_S - 30


def test_descarga_por_partes_a_disco(monkeypatch):
    monkeypatch.setattr(worker.requests, "get", lambda *a, **k: Descarga([b"a" * 10, b"b" * 10]))
    ruta = worker.download_audio("http://x/tema.mp3?token=1")
    assert open(ruta, "rb").read() == b"a" * 10 + b"b" * 10 and ruta.endswith(".mp3")
    os.remove(ruta)


def test_descarga_rechaza_por_content_length(monkeypatch):
    monkeypatch.setattr(worker, "MAX_TRACK_MB", 1)
    monkeypatch.setattr(worker.requests, "get", lambda *a, **k: Descarga([], largo=5 * 1024 * 1024))
    with pytest.raises(worker.ArchivoMuyGrande):
        worker.download_audio("http://x/a.wav")


def test_descarga_corta_si_pasa_el_tope_sin_content_length(monkeypatch, tmp_path):
    monkeypatch.setattr(worker, "MAX_TRACK_MB", 1)
    monkeypatch.setattr(worker.tempfile, "tempdir", str(tmp_path))
    mb = b"x" * (1024 * 1024)
    monkeypatch.setattr(worker.requests, "get", lambda *a, **k: Descarga([mb, mb]))
    with pytest.raises(worker.ArchivoMuyGrande):
        worker.download_audio("http://x/a.wav")
    assert os.listdir(tmp_path) == []          # no deja el temporal a medias


def test_un_trabajo_colgado_se_corta_y_avisa(monkeypatch):
    enviados = []
    monkeypatch.setattr(worker, "TOPE_TRABAJO_S", 1)
    monkeypatch.setattr(worker, "download_audio", lambda url: (time.sleep(5), "/no/existe")[1])
    monkeypatch.setattr(worker, "send_result", lambda *a, **k: enviados.append((a, k)))
    t0 = time.time()
    worker.process_job({"id": "j", "track_id": "t"}, {}, "http://x/a.mp3")
    assert time.time() - t0 < 3                    # la descarga también entra en el tope
    assert enviados and enviados[0][0][2] == "error" and "min" in enviados[0][1]["error"]


def test_no_anchor_es_carrera_no_falla_definitiva():
    assert gv.codigo_error(RuntimeError("no_anchor")) == "esperando_analisis:no_anchor"
    assert not gv.codigo_error(RuntimeError("no_anchor")).startswith("determinista:")


def test_archivo_muy_grande_va_como_determinista(monkeypatch, capsys):
    enviados = []
    def grande(url):
        raise worker.ArchivoMuyGrande("archivo de 300 MB (tope 250 MB)")
    monkeypatch.setattr(worker, "download_audio", grande)
    monkeypatch.setattr(worker, "send_result", lambda *a, **k: enviados.append((a, k)))
    worker.process_job({"id": "j", "track_id": "t"}, {}, "http://x/a.wav")
    assert enviados[0][1]["error"] == "determinista:archivo de 300 MB (tope 250 MB)"
    assert "FALLO en 0 s" in capsys.readouterr().out


def test_otros_errores_siguen_reintentandose(monkeypatch):
    enviados = []
    def red(url):
        raise worker.requests.ConnectionError("sin red")
    monkeypatch.setattr(worker, "download_audio", red)
    monkeypatch.setattr(worker, "send_result", lambda *a, **k: enviados.append((a, k)))
    worker.process_job({"id": "j", "track_id": "t"}, {}, "http://x/a.wav")
    assert not enviados[0][1]["error"].startswith("determinista:")


def test_el_log_dice_cuanto_tardo_el_trabajo(monkeypatch, capsys, tmp_path):
    p = tmp_path / "a.wav"
    p.write_bytes(b"x")
    monkeypatch.setattr(worker, "download_audio", lambda url: str(p))
    monkeypatch.setattr(worker, "analyze", lambda path, bpm_seed=None: {"energy": 7, "duration_seconds": 412})
    monkeypatch.setattr(worker, "detectar_genero", lambda path: {})
    monkeypatch.setattr(worker, "compute_loudness_lufs", lambda path: None)
    monkeypatch.setattr(worker, "send_result", lambda *a, **k: None)
    worker.process_job({"id": "j", "track_id": "t"}, {"artist": "a", "title": "b"}, "http://x/a.wav")
    assert "OK en 0 s (tema de 412 s)" in capsys.readouterr().out
