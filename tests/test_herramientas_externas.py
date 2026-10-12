"""Fallos de las herramientas externas (ffprobe, ffmpeg, la subida y worker-result), sin ffmpeg.
Las pruebas con ffmpeg de verdad (test_archivos_raros, test_master_flac, test_stream_sin_etiquetas)
solo corren en el CI; aquí se simula cada respuesta para cubrir en cualquier máquina el camino de
error: un archivo raro termina en `determinista:` y una herramienta que falla nunca tumba el trabajo."""
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import soundfile as sf  # noqa: E402
import worker  # noqa: E402

SR = 44100


def _proc(returncode=0, stdout=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


def _con_ffprobe(monkeypatch, respuesta):
    """sondear_audio con un ffprobe falso: `respuesta` es un proceso o una excepción."""
    monkeypatch.setattr("shutil.which", lambda nombre: "/usr/bin/" + nombre)

    def run(*a, **k):
        if isinstance(respuesta, BaseException):
            raise respuesta
        return respuesta
    monkeypatch.setattr(worker.subprocess, "run", run)


def _json(duracion=None, codec="mp3", flujos=True):
    info = {"streams": [{"codec_name": codec}] if flujos else []}
    if duracion is not None:
        info["format"] = {"duration": duracion}
    return json.dumps(info)


# ---------------------------------------------------------------- sondear_audio (ffprobe)

@pytest.mark.sondeo_real
def test_sondeo_devuelve_duracion_y_codec(monkeypatch):
    _con_ffprobe(monkeypatch, _proc(stdout=_json("245.3", "flac")))
    assert worker.sondear_audio("x.flac") == (245.3, "flac")


@pytest.mark.sondeo_real
@pytest.mark.parametrize("crudo", [None, "", "N/A", "abc"])
def test_sondeo_sin_duracion_legible_no_rechaza(monkeypatch, crudo):
    # Un stream sin duración en la cabecera (p. ej. un MP3 sin Xing) se deja pasar: lo mide librosa.
    _con_ffprobe(monkeypatch, _proc(stdout=_json(crudo)))
    assert worker.sondear_audio("x.mp3") == (None, "mp3")


@pytest.mark.sondeo_real
@pytest.mark.parametrize("dur", ["0", "0.000000", "0.99"])
def test_sondeo_rechaza_audio_de_menos_de_un_segundo(monkeypatch, dur):
    _con_ffprobe(monkeypatch, _proc(stdout=_json(dur)))
    with pytest.raises(worker.ArchivoIlegible, match="menos de 1 segundo"):
        worker.sondear_audio("x.wav")


@pytest.mark.sondeo_real
def test_sondeo_un_segundo_justo_se_acepta(monkeypatch):
    _con_ffprobe(monkeypatch, _proc(stdout=_json("1.0")))
    assert worker.sondear_audio("x.wav") == (1.0, "mp3")


@pytest.mark.sondeo_real
@pytest.mark.parametrize("respuesta", [
    _proc(returncode=1, stdout=_json("200")),         # ffprobe falla aunque escriba algo
    _proc(stdout=_json("200", flujos=False)),          # un archivo sin audio (p. ej. una imagen)
    _proc(stdout="esto no es json"),                   # salida rota
    _proc(stdout=""),                                  # nada
    subprocess.TimeoutExpired(cmd="ffprobe", timeout=60),
], ids=["codigo-1", "sin-flujo-de-audio", "json-roto", "salida-vacia", "se-cuelga"])
def test_sondeo_archivo_ilegible(monkeypatch, respuesta):
    _con_ffprobe(monkeypatch, respuesta)
    with pytest.raises(worker.ArchivoIlegible) as e:
        worker.sondear_audio("x.mp3")
    assert str(e.value) == worker.MSJ_ILEGIBLE
    assert "Expórtalo de nuevo" in str(e.value)        # el DJ sabe qué hacer


@pytest.mark.sondeo_real
def test_sondeo_sin_ffprobe_instalado_no_sondea(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda nombre: None)
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **k: pytest.fail("no debe llamar a ffprobe"))
    assert worker.sondear_audio("x.mp3") is None


def test_archivo_ilegible_es_un_error_determinista():
    # Sin reintentos eternos: process_job lo manda como `determinista:` (ver test_archivos_raros).
    assert issubclass(worker.ArchivoIlegible, RuntimeError)
    assert "{mb}" in worker.MSJ_MUY_GRANDE and "{tope}" in worker.MSJ_MUY_GRANDE


# ---------------------------------------------------------------- ffmpeg: renditions y FLAC

def test_rendition_devuelve_none_si_ffmpeg_falla(monkeypatch, tmp_path):
    src = tmp_path / "tema.wav"
    src.write_bytes(b"x")
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **k: _proc(returncode=1))
    assert worker.make_rendition(str(src)) is None


def test_rendition_devuelve_none_si_el_mp3_sale_vacio(monkeypatch, tmp_path):
    src = tmp_path / "tema.wav"
    src.write_bytes(b"x")

    def run(cmd, **k):
        open(cmd[-1], "wb").close()                    # ffmpeg «termina bien» pero sin bytes
        return _proc()
    monkeypatch.setattr(worker.subprocess, "run", run)
    assert worker.make_rendition(str(src)) is None


@pytest.mark.parametrize("error", [FileNotFoundError("ffmpeg"), subprocess.TimeoutExpired("ffmpeg", 180)])
def test_rendition_sin_ffmpeg_o_colgado_devuelve_none(monkeypatch, tmp_path, error):
    def run(*a, **k):
        raise error
    monkeypatch.setattr(worker.subprocess, "run", run)
    assert worker.make_rendition(str(tmp_path / "tema.wav")) is None


def test_rendition_de_escucha_sin_etiquetas_y_descarga_con_ellas(monkeypatch, tmp_path):
    src = tmp_path / "tema.wav"
    src.write_bytes(b"x")
    comandos = []

    def run(cmd, **k):
        comandos.append(cmd)
        with open(cmd[-1], "wb") as f:
            f.write(b"mp3")
        return _proc()
    monkeypatch.setattr(worker.subprocess, "run", run)
    escucha = worker.make_rendition(str(src))
    descarga = worker.make_rendition(str(src), bitrate="320k", sufijo=".master", etiquetas=True)
    assert escucha == str(src) + ".stream.mp3" and descarga == str(src) + ".master.mp3"
    sin, con = comandos
    assert sin[sin.index("-map_metadata") + 1] == "-1" and "+bitexact" in sin   # nombre real oculto
    assert con[con.index("-map_metadata") + 1] == "0"
    assert sin[sin.index("-b:a") + 1] == "192k" and con[con.index("-b:a") + 1] == "320k"
    assert "-vn" in sin and "-write_xing" in sin       # sin portada y con duración fiable


def test_codec_de_sin_ffprobe_o_sin_salida(monkeypatch):
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **k: _proc(stdout="\n"))
    assert worker.codec_de("x.wav") is None

    def falla(*a, **k):
        raise FileNotFoundError("ffprobe")
    monkeypatch.setattr(worker.subprocess, "run", falla)
    assert worker.codec_de("x.wav") is None


def test_codec_de_lee_el_nombre(monkeypatch):
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **k: _proc(stdout="pcm_s24le\n"))
    assert worker.codec_de("x.wav") == "pcm_s24le" and "pcm_s24le" in worker.CODECS_PCM_ENTERO


def _wav(ruta, x, sr=SR, subtipo="PCM_16"):
    sf.write(str(ruta), x, sr, subtype=subtipo)
    return str(ruta)


def test_mismo_audio_compara_formato_largo_y_muestras(tmp_path):
    x = (np.sin(np.arange(SR) / 10) * 0.5).astype(np.float32)
    a = _wav(tmp_path / "a.wav", x)
    assert worker.mismo_audio(a, _wav(tmp_path / "igual.wav", x))
    assert not worker.mismo_audio(a, _wav(tmp_path / "otro_sr.wav", x, sr=48000))
    assert not worker.mismo_audio(a, _wav(tmp_path / "corto.wav", x[:-1]))
    assert not worker.mismo_audio(a, _wav(tmp_path / "estereo.wav", np.stack([x, x], axis=1)))
    assert not worker.mismo_audio(a, str(tmp_path / "no-existe.flac"))   # ilegible: no es igual


def test_mismo_audio_archivo_vacio_contra_si_mismo(tmp_path):
    a = _wav(tmp_path / "vacio.wav", np.zeros(0, dtype=np.float32))
    assert worker.mismo_audio(a, a)


def test_master_flac_no_identico_deja_el_original(monkeypatch, tmp_path):
    src = tmp_path / "tema.wav"
    src.write_bytes(b"x")
    llamadas = []

    def run(cmd, **k):
        llamadas.append(cmd)
        with open(cmd[-1], "wb") as f:
            f.write(b"flac")
        return _proc()
    monkeypatch.setattr(worker.subprocess, "run", run)
    monkeypatch.setattr(worker, "mismo_audio", lambda a, b: False)
    assert worker.make_master_flac(str(src)) is None
    assert len(llamadas) == 1                          # no reintenta: el audio salió distinto
    assert not os.path.exists(str(src) + ".master.flac") and src.exists()


def test_master_flac_reintenta_sin_portada(monkeypatch, tmp_path):
    src = tmp_path / "tema.wav"
    src.write_bytes(b"x")
    llamadas = []

    def run(cmd, **k):
        llamadas.append(cmd)
        if "attached_pic" in cmd:                      # portada que FLAC no acepta
            return _proc(returncode=1)
        with open(cmd[-1], "wb") as f:
            f.write(b"flac")
        return _proc()
    monkeypatch.setattr(worker.subprocess, "run", run)
    monkeypatch.setattr(worker, "mismo_audio", lambda a, b: True)
    assert worker.make_master_flac(str(src)) == str(src) + ".master.flac"
    assert len(llamadas) == 2 and "-vn" in llamadas[1]
    assert all("-map_metadata" in c for c in llamadas)  # metadatos copiados en los dos intentos


def test_master_flac_si_los_dos_intentos_fallan_no_deja_basura(monkeypatch, tmp_path):
    src = tmp_path / "tema.wav"
    src.write_bytes(b"x")

    def run(cmd, **k):
        open(cmd[-1], "wb").close()                    # deja un archivo vacío y falla
        if "attached_pic" in cmd:
            return _proc(returncode=1)
        raise subprocess.TimeoutExpired("ffmpeg", 600)
    monkeypatch.setattr(worker.subprocess, "run", run)
    assert worker.make_master_flac(str(src)) is None
    assert os.listdir(tmp_path) == ["tema.wav"]


# ---------------------------------------------------------------- subida a Storage

class _Resp:
    def __init__(self, status):
        self.status_code = status


@pytest.mark.parametrize("status,ok", [(200, True), (201, True), (400, False), (403, False), (500, False)])
def test_subida_solo_cuenta_200_y_201(monkeypatch, tmp_path, status, ok):
    p = tmp_path / "a.mp3"
    p.write_bytes(b"abc")
    monkeypatch.setattr(worker.requests, "put", lambda *a, **k: _Resp(status))
    assert worker.upload_rendition("http://s/firmada?token=x", str(p)) is ok


def test_subida_manda_bytes_tipo_y_upsert(monkeypatch, tmp_path):
    p = tmp_path / "a.flac"
    p.write_bytes(b"datos")
    vistos = {}

    def put(url, data=None, headers=None, timeout=None):
        vistos.update(url=url, data=data, headers=headers)
        return _Resp(200)
    monkeypatch.setattr(worker.requests, "put", put)
    assert worker.upload_rendition("http://s/f", str(p))
    assert vistos["data"] == b"datos" and vistos["headers"]["content-type"] == "audio/mpeg"
    assert vistos["headers"]["x-upsert"] == "true"
    assert worker.upload_rendition("http://s/f", str(p), mime=worker.MIME_FLAC)
    assert vistos["headers"]["content-type"] == "audio/flac"


def test_subida_con_error_de_red_o_sin_archivo_devuelve_false(monkeypatch, tmp_path):
    def caida(*a, **k):
        raise worker.requests.ConnectionError("sin red")
    monkeypatch.setattr(worker.requests, "put", caida)
    p = tmp_path / "a.mp3"
    p.write_bytes(b"abc")
    assert worker.upload_rendition("http://s/f", str(p)) is False
    monkeypatch.setattr(worker.requests, "put", lambda *a, **k: _Resp(200))
    assert worker.upload_rendition("http://s/f", str(tmp_path / "no-existe.mp3")) is False


# ---------------------------------------------------------------- descarga del original

class _Descarga:
    def __init__(self, partes, falla_en=None):
        self.partes, self.falla_en, self.headers = partes, falla_en, {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=None):
        for i, parte in enumerate(self.partes):
            if i == self.falla_en:
                raise worker.requests.ConnectionError("se cortó la descarga")
            yield parte


def test_descarga_cortada_a_medias_no_deja_temporal(monkeypatch, tmp_path):
    monkeypatch.setattr(worker.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(worker.requests, "get", lambda *a, **k: _Descarga([b"a" * 10, b"b"], falla_en=1))
    with pytest.raises(worker.requests.ConnectionError):
        worker.download_audio("http://x/tema.wav?token=1")
    assert os.listdir(tmp_path) == []


def test_descarga_sin_extension_usa_audio(monkeypatch):
    monkeypatch.setattr(worker.requests, "get", lambda *a, **k: _Descarga([b"abc"]))
    ruta = worker.download_audio("http://x/objeto-sin-extension?token=1")
    try:
        assert ruta.endswith(".audio") and open(ruta, "rb").read() == b"abc"
    finally:
        os.remove(ruta)


def test_descarga_con_error_http_no_crea_temporal(monkeypatch, tmp_path):
    class _Negada(_Descarga):
        def raise_for_status(self):
            raise worker.requests.HTTPError("403 la firma venció")
    monkeypatch.setattr(worker.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(worker.requests, "get", lambda *a, **k: _Negada([b"x"]))
    with pytest.raises(worker.requests.HTTPError):
        worker.download_audio("http://x/a.mp3")
    assert os.listdir(tmp_path) == []


# ---------------------------------------------------------------- worker-result

class _Post:
    def __init__(self, status=200):
        self.status = status
        self.enviado = None

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.enviado = {"url": url, "headers": headers, "json": json}
        return self

    def raise_for_status(self):
        if self.status >= 400:
            raise worker.requests.HTTPError(f"{self.status}")


def test_resultado_lleva_el_resultado_y_el_secreto(monkeypatch):
    post = _Post()
    monkeypatch.setattr(worker.requests, "post", post)
    worker.send_result("j1", "t1", "done", result={"bpm": 124})
    assert post.enviado["url"].endswith("/worker-result")
    assert post.enviado["json"] == {"job_id": "j1", "track_id": "t1", "status": "done", "result": {"bpm": 124}}
    assert post.enviado["headers"]["x-worker-secret"] == worker.WORKER_SECRET


def test_resultado_con_error_tapa_firmas_y_recorta(monkeypatch):
    post = _Post()
    monkeypatch.setattr(worker.requests, "post", post)
    error = "403 al bajar https://s/obj.mp3?token=abc123secreto&x=1 " + "z" * 2000
    worker.send_result("j1", "t1", "error", error=error)
    enviado = post.enviado["json"]
    assert "result" not in enviado and "abc123secreto" not in enviado["error"]
    assert "token=***" in enviado["error"] and len(enviado["error"]) == 1000


def test_resultado_sin_error_ni_resultado_manda_lo_minimo(monkeypatch):
    post = _Post()
    monkeypatch.setattr(worker.requests, "post", post)
    worker.send_result("j1", "t1", "processing", error="")
    assert post.enviado["json"] == {"job_id": "j1", "track_id": "t1", "status": "processing"}


def test_resultado_rechazado_por_el_servidor_se_propaga(monkeypatch):
    # process_job lo atrapa y avisa el error en su lugar: el trabajo no queda «entregado» en silencio.
    monkeypatch.setattr(worker.requests, "post", _Post(status=500))
    with pytest.raises(worker.requests.HTTPError):
        worker.send_result("j1", "t1", "done", result={})
