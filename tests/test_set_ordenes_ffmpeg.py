"""Órdenes a ffmpeg del render de sets y de la lectura de etiquetas, sin ffmpeg instalado.
Las pruebas con ffmpeg de verdad (test_set_plan, test_genero) solo corren en el CI y miden el
sonido; aquí se espía `subprocess.run` para fijar en cualquier máquina la orden exacta y el
camino de respaldo: rubberband -> atempo -> sin estirar. También `_set_api` y la red caída al
bajar un tema."""
import os
import subprocess
import sys
from types import SimpleNamespace

import numpy.testing  # noqa: F401 — numpy lo carga perezoso y al importarse llama a subprocess.run
import numpy as np
import pytest
import requests

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import worker  # noqa: E402

SR = worker.SET_SR


class FFmpeg:
    """ffmpeg falso: guarda cada orden y responde con `salida`; los filtros de `fallan` dan error."""

    def __init__(self, monkeypatch, salida=b"", fallan=()):
        self.ordenes, self.opciones, self.salida, self.fallan = [], [], salida, fallan
        monkeypatch.setattr(worker.subprocess, "run", self)

    def __call__(self, cmd, **kw):
        self.ordenes.append(list(cmd))
        self.opciones.append(kw)
        filtro = cmd[cmd.index("-af") + 1] if "-af" in cmd else ""
        if filtro.split("=")[0] in self.fallan:
            raise subprocess.CalledProcessError(1, cmd)
        return SimpleNamespace(returncode=0, stdout=self.salida, stderr="")

    def filtros(self):
        return [o[o.index("-af") + 1] for o in self.ordenes]


def pcm(n, valor=0.25):
    return np.full((n, 2), valor, dtype=np.float32)


# ---------------------------------------------------------------- _decode_pcm

def test_decode_pide_float32_estereo_al_sr_del_set(monkeypatch):
    ff = FFmpeg(monkeypatch, salida=pcm(5).tobytes())
    audio = worker._decode_pcm("tema.mp3")
    assert ff.ordenes == [["ffmpeg", "-v", "error", "-i", "tema.mp3", "-f", "f32le", "-acodec", "pcm_f32le",
                           "-ar", str(SR), "-ac", "2", "-"]]
    assert ff.opciones[0] == {"capture_output": True, "check": True}
    assert audio.shape == (5, 2) and audio.dtype == np.float32
    np.testing.assert_array_equal(audio, pcm(5))


def test_decode_devuelve_una_copia_que_se_puede_modificar(monkeypatch):
    # np.frombuffer sobre bytes es de solo lectura: el render escribe encima (fundidos, ganancia).
    FFmpeg(monkeypatch, salida=pcm(3).tobytes())
    audio = worker._decode_pcm("tema.mp3")
    audio *= 2
    assert audio[0, 0] == pytest.approx(0.5)


def test_decode_respeta_otro_sr(monkeypatch):
    ff = FFmpeg(monkeypatch, salida=pcm(2).tobytes())
    worker._decode_pcm("tema.flac", sr=22050)
    assert ff.ordenes[0][ff.ordenes[0].index("-ar") + 1] == "22050"


def test_decode_archivo_que_ffmpeg_no_abre_lanza_el_error(monkeypatch):
    def run(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd)
    monkeypatch.setattr(worker.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        worker._decode_pcm("roto.mp3")


# ---------------------------------------------------------------- _stretch (archivo completo)

@pytest.mark.parametrize("ratio", [1.0, 1.00005, 0.99995])
def test_stretch_sin_cambio_de_tempo_no_llama_a_ffmpeg(monkeypatch, tmp_path, ratio):
    ff = FFmpeg(monkeypatch)
    assert worker._stretch("tema.mp3", ratio, str(tmp_path)) == "tema.mp3"
    assert ff.ordenes == []


def test_stretch_usa_rubberband_y_deja_un_wav_en_la_carpeta_temporal(monkeypatch, tmp_path):
    ff = FFmpeg(monkeypatch)
    dst = worker._stretch("tema.mp3", 1.02, str(tmp_path))
    assert ff.filtros() == ["rubberband=tempo=1.020000"]
    assert os.path.dirname(dst) == str(tmp_path) and dst.endswith(".wav")
    orden = ff.ordenes[0]
    assert orden[:6] == ["ffmpeg", "-v", "error", "-y", "-i", "tema.mp3"]
    assert orden[-5:] == ["-ar", str(SR), "-ac", "2", dst]


def test_stretch_sin_rubberband_cae_a_atempo(monkeypatch, tmp_path):
    ff = FFmpeg(monkeypatch, fallan=("rubberband",))
    dst = worker._stretch("tema.mp3", 0.97, str(tmp_path))
    assert ff.filtros() == ["rubberband=tempo=0.970000", "atempo=0.970000"]
    assert dst != "tema.mp3" and dst == ff.ordenes[1][-1]


def test_stretch_si_fallan_los_dos_filtros_devuelve_el_original(monkeypatch, tmp_path):
    # El set se rinde igual, con ese tema sin estirar: no se cae el trabajo.
    ff = FFmpeg(monkeypatch, fallan=("rubberband", "atempo"))
    assert worker._stretch("tema.mp3", 1.03, str(tmp_path)) == "tema.mp3"
    assert len(ff.ordenes) == 2


def test_stretch_dos_temas_no_comparten_archivo(monkeypatch, tmp_path):
    FFmpeg(monkeypatch)
    a = worker._stretch("a.mp3", 1.02, str(tmp_path))
    b = worker._stretch("b.mp3", 1.02, str(tmp_path))
    c = worker._stretch("a.mp3", 1.03, str(tmp_path))
    assert len({a, b, c}) == 3


# ---------------------------------------------------------------- _estirar_pcm (bloque de la mezcla)

@pytest.mark.parametrize("tempo", [1.0, 1.00005])
def test_estirar_pcm_sin_cambio_devuelve_el_mismo_bloque(monkeypatch, tmp_path, tempo):
    ff = FFmpeg(monkeypatch)
    x = pcm(100)
    assert worker._estirar_pcm(x, tempo, str(tmp_path)) is x
    assert ff.ordenes == []


def test_estirar_pcm_bloque_vacio_no_llama_a_ffmpeg(monkeypatch, tmp_path):
    ff = FFmpeg(monkeypatch)
    x = np.zeros((0, 2), dtype=np.float32)
    assert worker._estirar_pcm(x, 1.05, str(tmp_path)) is x
    assert ff.ordenes == []


def test_estirar_pcm_manda_el_bloque_crudo_y_lee_el_resultado(monkeypatch, tmp_path):
    ff = FFmpeg(monkeypatch, salida=pcm(80, 0.5).tobytes())
    x = pcm(100).astype(np.float64)  # llega en otro tipo: al archivo va en float32
    sal = worker._estirar_pcm(x, 1.25, str(tmp_path))
    orden = ff.ordenes[0]
    src = orden[orden.index("-i") + 1]
    # Entrada y salida crudas, estéreo y al SR del set: sin cabeceras que desplacen el audio.
    assert orden[:9] == ["ffmpeg", "-v", "error", "-f", "f32le", "-ar", str(SR), "-ac", "2"]
    assert orden[-7:] == ["-f", "f32le", "-ar", str(SR), "-ac", "2", "-"]
    assert ff.filtros() == ["rubberband=tempo=1.250000"]
    assert os.path.dirname(src) == str(tmp_path)
    np.testing.assert_array_equal(np.fromfile(src, dtype=np.float32).reshape(-1, 2), pcm(100))
    assert sal.shape == (80, 2) and sal.dtype == np.float32 and sal.flags.writeable
    np.testing.assert_array_equal(sal, pcm(80, 0.5))


def test_estirar_pcm_sin_rubberband_cae_a_atempo(monkeypatch, tmp_path):
    ff = FFmpeg(monkeypatch, salida=pcm(10).tobytes(), fallan=("rubberband",))
    sal = worker._estirar_pcm(pcm(12), 1.2, str(tmp_path))
    assert ff.filtros() == ["rubberband=tempo=1.200000", "atempo=1.200000"]
    assert sal.shape == (10, 2)


def test_estirar_pcm_si_fallan_los_dos_avisa_y_devuelve_none(monkeypatch, tmp_path, capsys):
    # None es la señal para que la mezcla use el corte limpio (SinEstirar) en vez de desafinar.
    FFmpeg(monkeypatch, fallan=("rubberband", "atempo"))
    assert worker._estirar_pcm(pcm(12), 0.9, str(tmp_path)) is None
    assert "no se pudo estirar a tempo 0.9000" in capsys.readouterr().out


# ---------------------------------------------------------------- _set_api

class Respuesta:
    def __init__(self, status=200, cuerpo=None):
        self.status_code, self.cuerpo = status, cuerpo or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self.cuerpo


def con_post(monkeypatch, respuesta):
    llamadas = []

    def post(url, **kw):
        llamadas.append((url, kw))
        return respuesta
    monkeypatch.setattr(worker.requests, "post", post)
    return llamadas


def test_set_api_manda_el_secreto_y_devuelve_el_json(monkeypatch):
    llamadas = con_post(monkeypatch, Respuesta(cuerpo={"job": {"id": "j1"}}))
    assert worker._set_api("result", {"job_id": "j1"}) == {"job": {"id": "j1"}}
    url, kw = llamadas[0]
    assert url == f"{worker.WORKER_API_URL}/set-render?action=result"
    assert kw["headers"] == {"x-worker-secret": worker.WORKER_SECRET, "Content-Type": "application/json"}
    assert kw["json"] == {"job_id": "j1"} and kw["timeout"] == 120


def test_set_api_sin_cuerpo_manda_un_objeto_vacio(monkeypatch):
    llamadas = con_post(monkeypatch, Respuesta())
    worker._set_api("next")
    assert llamadas[0][1]["json"] == {}


@pytest.mark.parametrize("status", [401, 404, 500])
def test_set_api_error_http_se_lanza(monkeypatch, status):
    con_post(monkeypatch, Respuesta(status=status))
    with pytest.raises(requests.HTTPError):
        worker._set_api("next")


# ---------------------------------------------------------------- _bajar_tema

@pytest.mark.parametrize("error", [requests.ConnectionError, requests.Timeout])
def test_bajar_tema_con_la_red_caida_no_muestra_la_url_firmada(monkeypatch, tmp_path, error):
    def get(url, **kw):
        raise error(f"no se pudo abrir {url}")
    monkeypatch.setattr(worker.requests, "get", get)
    dest = tmp_path / "t.mp3"
    with pytest.raises(RuntimeError) as e:
        worker._bajar_tema("https://x.supabase.co/storage/v1/object/sign/music/a.mp3?token=eyJsecreto", str(dest))
    assert str(e.value) == f"descarga fallida ({error.__name__}) desde x.supabase.co"
    # `from None`: el error original, que trae la URL con su token, no viaja en la traza.
    assert e.value.__cause__ is None and e.value.__suppress_context__
    assert not dest.exists()


def test_bajar_tema_sin_url_dice_que_no_hay_host(monkeypatch, tmp_path):
    def get(url, **kw):
        raise requests.exceptions.MissingSchema("sin url")
    monkeypatch.setattr(worker.requests, "get", get)
    with pytest.raises(RuntimeError, match=r"descarga fallida \(MissingSchema\) desde \?"):
        worker._bajar_tema(None, str(tmp_path / "t.mp3"))


# ---------------------------------------------------------------- leer_etiquetas y detectar_genero

FFMETADATA = """;FFMETADATA1
title=Un tema = otro
ARTIST= Alguien
Genre=Tech House
genre=Pop
; un comentario=que no cuenta
linea sin igual
encoder=Lavf60
"""


def con_etiquetas(monkeypatch, salida):
    ordenes = []

    def run(cmd, **kw):
        ordenes.append((list(cmd), kw))
        if isinstance(salida, BaseException):
            raise salida
        return SimpleNamespace(returncode=0, stdout=salida, stderr="")
    monkeypatch.setattr(worker.subprocess, "run", run)
    return ordenes


def test_leer_etiquetas_pide_ffmetadata_con_tope_de_tiempo(monkeypatch):
    ordenes = con_etiquetas(monkeypatch, FFMETADATA)
    worker.leer_etiquetas("tema.mp3")
    cmd, kw = ordenes[0]
    assert cmd == ["ffmpeg", "-v", "error", "-i", "tema.mp3", "-f", "ffmetadata", "-"]
    assert kw == {"capture_output": True, "text": True, "timeout": 30}


def test_leer_etiquetas_claves_en_minuscula_y_gana_la_primera(monkeypatch):
    con_etiquetas(monkeypatch, FFMETADATA)
    assert worker.leer_etiquetas("tema.mp3") == {
        "title": "Un tema = otro",   # solo parte en el primer «=»
        "artist": "Alguien",
        "genre": "Tech House",       # la segunda «genre» no la pisa
        "encoder": "Lavf60",
    }


@pytest.mark.parametrize("salida", ["", None, ";FFMETADATA1\n"])
def test_leer_etiquetas_sin_etiquetas_da_vacio(monkeypatch, salida):
    con_etiquetas(monkeypatch, salida)
    assert worker.leer_etiquetas("tema.wav") == {}


@pytest.mark.parametrize("error", [FileNotFoundError("ffmpeg"), subprocess.TimeoutExpired("ffmpeg", 30),
                                   OSError("sin memoria")])
def test_leer_etiquetas_si_ffmpeg_falla_da_vacio(monkeypatch, error):
    con_etiquetas(monkeypatch, error)
    assert worker.leer_etiquetas("tema.mp3") == {}


def test_genero_reconocido_de_la_etiqueta(monkeypatch):
    con_etiquetas(monkeypatch, "genre=tech house\n")
    assert worker.detectar_genero("tema.mp3") == {
        "genre_detected": "Tech House", "genre_confidence": 0.95, "genre_source": "etiqueta"}


def test_genero_de_tcon_cuando_no_hay_genre(monkeypatch):
    # ID3 sin traducir: la etiqueta llega como TCON y con el número de ID3v1 entre paréntesis.
    con_etiquetas(monkeypatch, "TCON=(18)\n")
    g = worker.detectar_genero("tema.mp3")
    assert g["genre_detected"] == "Techno (Peak Time / Driving)" and g["genre_source"] == "etiqueta"
    assert "genero_por_revisar" not in g


def test_genero_amplio_queda_por_revisar(monkeypatch):
    con_etiquetas(monkeypatch, "genre=Electronic\n")
    assert worker.detectar_genero("tema.mp3") == {
        "genre_detected": "Electronic", "genre_confidence": 0.3, "genre_source": "etiqueta",
        "genero_por_revisar": True}


def test_genero_desconocido_se_respeta_pero_por_revisar(monkeypatch):
    con_etiquetas(monkeypatch, "genre=Guaracha Aleteo\n")
    g = worker.detectar_genero("tema.mp3")
    assert g["genre_detected"] == "Guaracha Aleteo" and g["genre_confidence"] == 0.6
    assert g["genero_por_revisar"] is True


@pytest.mark.parametrize("salida", ["title=sin genero\n", "genre=\n", FileNotFoundError("ffmpeg")])
def test_sin_genero_no_inventa_y_queda_por_revisar(monkeypatch, salida):
    con_etiquetas(monkeypatch, salida)
    assert worker.detectar_genero("tema.mp3") == {
        "genre_detected": None, "genre_confidence": 0.0, "genre_source": None, "genero_por_revisar": True}
