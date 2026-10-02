"""#265 (privacidad): la copia de escucha no lleva título, artista ni portada; el master de
descarga sí conserva sus etiquetas; las copias viejas se limpian sin re-codificar."""
import os
import shutil
import subprocess
import sys

import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import worker  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="sin ffmpeg")
SECRETO = b"Love House Secreta"


def mp3_con_etiquetas(tmp_path, portada=True):
    """MP3 de 3 s con título, artista, comentario e (opcional) portada embebida."""
    wav = tmp_path / "tono.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                    "-ac", "2", "-ar", "44100", str(wav)], check=True)
    out = tmp_path / "con_etiquetas.mp3"
    entradas = ["-i", str(wav)]
    mapas = ["-map", "0:a"]
    if portada:
        png = tmp_path / "portada.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x64",
                        "-frames:v", "1", str(png)], check=True)
        entradas += ["-i", str(png)]
        mapas += ["-map", "1:v", "-c:v", "png", "-disposition:v", "attached_pic"]
    subprocess.run(["ffmpeg", "-v", "error", "-y", *entradas, *mapas, "-c:a", "libmp3lame", "-b:a", "128k",
                    "-id3v2_version", "3", "-write_id3v1", "1",
                    "-metadata", f"title={SECRETO.decode()}", "-metadata", "artist=Oliver Secreto",
                    "-metadata", "comment=8A - 7", str(out)], check=True)
    data = out.read_bytes()
    assert data[:3] == b"ID3" and SECRETO in data  # la muestra sí trae la fuga
    return str(out)


def duracion(path):
    p = subprocess.run(["ffmpeg", "-i", path, "-map", "0:a", "-f", "null", "-"], capture_output=True, text=True)
    t = [ln for ln in p.stderr.splitlines() if "time=" in ln][-1]
    h, m, s = t.split("time=")[1].split()[0].split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def test_copia_de_escucha_sin_etiquetas_ni_portada(tmp_path):
    out = worker.make_rendition(mp3_con_etiquetas(tmp_path))
    data = open(out, "rb").read()
    assert data[:3] != b"ID3" and data[-128:-125] != b"TAG"
    assert SECRETO not in data and b"Oliver" not in data and b"APIC" not in data and b"PNG" not in data
    assert not worker.tiene_etiquetas(out)


def test_master_de_descarga_conserva_etiquetas(tmp_path):
    out = worker.make_rendition(mp3_con_etiquetas(tmp_path, portada=False), bitrate="320k",
                                sufijo=".master", etiquetas=True)
    assert SECRETO in open(out, "rb").read()


def test_limpiar_sin_recodificar(tmp_path):
    src = mp3_con_etiquetas(tmp_path)
    out = worker.limpiar_etiquetas(src, ".mp3")
    data = open(out, "rb").read()
    assert SECRETO not in data and b"APIC" not in data and not worker.tiene_etiquetas(out)
    assert duracion(out) == pytest.approx(duracion(src), abs=0.03)   # mismo audio, mismo timeline


class Plataforma:
    def __init__(self, archivo, existe=True):
        self.archivo, self.existe = archivo, existe
        self.llamadas, self.subido = [], None

    def post(self, url, headers=None, json=None, timeout=None):
        accion = url.split("action=")[1]
        self.llamadas.append((accion, json))
        return R(404 if not self.existe else 200, {"job": {"id": "j1", "track_id": "t1"},
                 "audio_url": "http://audio/x", "upload": {"url": "http://subir/x", "path": "dj/streams/x.mp3"}}
                 if accion == "next" else {"ok": True})

    def get(self, url, timeout=None):
        return R(200, content=open(self.archivo, "rb").read())

    def put(self, url, data=None, headers=None, timeout=None):
        self.subido = data.read()
        return R(200)


class R:
    def __init__(self, status, js=None, content=b""):
        self.status_code, self._js, self.content = status, js, content

    def json(self):
        return self._js

    def raise_for_status(self):
        if self.status_code >= 400:
            raise worker.requests.HTTPError(str(self.status_code))


def test_reproceso_limpia_y_sube_en_la_misma_ruta(tmp_path, monkeypatch):
    plat = Plataforma(mp3_con_etiquetas(tmp_path))
    monkeypatch.setattr(worker, "LIMPIEZA_DISPONIBLE", True)
    for m in ("post", "get", "put"):
        monkeypatch.setattr(worker.requests, m, getattr(plat, m))
    assert worker.poll_limpiar_streams() is True
    assert SECRETO not in plat.subido and plat.subido[:3] != b"ID3"
    accion, cuerpo = plat.llamadas[-1]
    assert accion == "result" and cuerpo["path"] == "dj/streams/x.mp3" and cuerpo["bytes"] == len(plat.subido)


def test_sin_funcion_en_la_plataforma_no_insiste(tmp_path, monkeypatch):
    plat = Plataforma(mp3_con_etiquetas(tmp_path), existe=False)
    monkeypatch.setattr(worker, "LIMPIEZA_DISPONIBLE", True)
    monkeypatch.setattr(worker.requests, "post", plat.post)
    assert worker.poll_limpiar_streams() is False
    assert worker.poll_limpiar_streams() is False
    assert len(plat.llamadas) == 1      # tras el 404 no vuelve a preguntar
