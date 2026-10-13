"""#265 (privacidad), sin ffmpeg: cómo se detectan las etiquetas, qué se le pide a ffmpeg para
quitarlas y qué hace la cola `stream-limpiar` cuando algo sale mal. Sin cambiar código.

tests/test_stream_sin_etiquetas.py prueba el camino bueno con ffmpeg de verdad (solo corre en
el CI). Aquí ffmpeg y la plataforma son dobles, así que corre en cualquier máquina."""
import os
import sys
import types

import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import worker  # noqa: E402

AUDIO = b"\xff\xfb\x90\x00" + b"\x00" * 600       # cuadros de MP3 de mentira, sin etiquetas
ID3V2 = b"ID3\x03\x00\x00\x00\x00\x00\x0a" + b"TIT2Secreta"
ID3V1 = b"TAG" + b"Secreta".ljust(125, b"\x00")   # los últimos 128 bytes del archivo


def archivo(tmp_path, datos, nombre="a.mp3"):
    p = tmp_path / nombre
    p.write_bytes(datos)
    return str(p)


# ----------------------------------------------------------------------------- tiene_etiquetas
@pytest.mark.parametrize("datos, esperado", [
    (AUDIO, False),
    (ID3V2 + AUDIO, True),                         # ID3v2 al principio
    (AUDIO + ID3V1, True),                         # ID3v1 al final
    (ID3V2 + AUDIO + ID3V1, True),
    (AUDIO + b"\xa9nam" + AUDIO, True),            # título en un MP4
    (AUDIO + b"\xa9ART" + AUDIO, True),            # artista en un MP4
    (AUDIO + b"covr" + AUDIO, True),               # portada en un MP4
    (b"", False),                                  # archivo vacío: no revienta
    (b"\xff\xfb", False),                          # más corto que una cabecera
    (AUDIO + b"TAG" + AUDIO, False),               # "TAG" en medio del audio no es ID3v1
    (AUDIO + b"ID3" + AUDIO, False),               # "ID3" en medio del audio no es ID3v2
], ids=["limpio", "id3v2", "id3v1", "las dos", "mp4 titulo", "mp4 artista", "mp4 portada",
        "vacio", "muy corto", "TAG en medio", "ID3 en medio"])
def test_tiene_etiquetas(tmp_path, datos, esperado):
    assert worker.tiene_etiquetas(archivo(tmp_path, datos)) is esperado


def test_id3v1_solo_cuenta_en_los_ultimos_128_bytes(tmp_path):
    assert worker.tiene_etiquetas(archivo(tmp_path, AUDIO + ID3V1)) is True
    assert worker.tiene_etiquetas(archivo(tmp_path, AUDIO + ID3V1 + b"\x00")) is False


def test_archivo_enorme_solo_se_mira_por_las_puntas(tmp_path):
    # Desde 64 MB no se lee el archivo entero para buscar átomos de MP4 (memoria): solo cuentan
    # la cabecera y la cola. El archivo de la prueba es disperso, no ocupa 64 MB en disco.
    p = tmp_path / "grande.m4a"
    with open(p, "wb") as f:
        f.write(AUDIO + b"\xa9nam")
        f.truncate(64 * 1024 * 1024)
    assert worker.tiene_etiquetas(str(p)) is False
    with open(p, "r+b") as f:
        f.truncate(64 * 1024 * 1024 - 1)
    assert worker.tiene_etiquetas(str(p)) is True


# ----------------------------------------------------------------------------- limpiar_etiquetas
@pytest.fixture
def ffmpeg(monkeypatch):
    """ffmpeg de mentira: guarda la orden y escribe en la salida lo que diga `escribe`."""
    f = types.SimpleNamespace(ordenes=[], opciones=[], rc=0, escribe=AUDIO)

    def run(cmd, **kw):
        f.ordenes.append(cmd)
        f.opciones.append(kw)
        if f.escribe is not None:
            with open(cmd[-1], "wb") as fh:
                fh.write(f.escribe)
        return types.SimpleNamespace(returncode=f.rc, stdout=b"", stderr=b"")

    monkeypatch.setattr(worker.subprocess, "run", run)
    return f


def test_limpiar_mp3_copia_el_audio_sin_recodificar(ffmpeg, tmp_path):
    src = archivo(tmp_path, ID3V2 + AUDIO)
    out = worker.limpiar_etiquetas(src, ".mp3")
    assert out == src + ".limpio.mp3" and open(out, "rb").read() == AUDIO
    cmd, = ffmpeg.ordenes
    assert cmd[0] == "ffmpeg" and cmd[-1] == out
    assert cmd[cmd.index("-i") + 1] == src
    assert cmd[cmd.index("-c:a") + 1] == "copy"            # sin recodificar: misma calidad y mismo timeline
    assert cmd[cmd.index("-map") + 1] == "0:a:0"           # solo el audio: la portada no pasa
    assert cmd[cmd.index("-map_metadata") + 1] == "-1"
    assert cmd[cmd.index("-id3v2_version") + 1] == "0" and cmd[cmd.index("-write_id3v1") + 1] == "0"
    assert cmd[cmd.index("-write_xing") + 1] == "1"        # conserva la cabecera con el retardo del codificador
    assert cmd[cmd.index("-f") + 1] == "mp3"
    assert ffmpeg.opciones[0]["timeout"] == 180            # un archivo trabado no cuelga la réplica


def test_limpiar_m4a_usa_el_contenedor_de_mp4(ffmpeg, tmp_path):
    src = archivo(tmp_path, AUDIO + b"\xa9nam", "a.m4a")
    out = worker.limpiar_etiquetas(src, ".m4a")
    cmd, = ffmpeg.ordenes
    assert out.endswith(".limpio.m4a")
    assert cmd[cmd.index("-f") + 1] == "ipod"
    assert cmd[cmd.index("-movflags") + 1] == "+faststart"  # empieza a sonar sin bajar todo el archivo
    assert "-write_xing" not in cmd                         # eso es solo de MP3
    assert cmd[cmd.index("-c:a") + 1] == "copy"


@pytest.mark.parametrize("rc, escribe, mensaje", [
    (1, AUDIO, "ffmpeg no pudo limpiar (1)"),
    (0, None, "ffmpeg no pudo limpiar (0)"),               # dijo que sí pero no dejó archivo
    (0, b"", "ffmpeg no pudo limpiar (0)"),                # dejó un archivo vacío
    (0, ID3V2 + AUDIO, "la copia limpia todavia tiene etiquetas"),
    (0, AUDIO + ID3V1, "la copia limpia todavia tiene etiquetas"),
], ids=["ffmpeg falla", "sin salida", "salida vacia", "queda id3v2", "queda id3v1"])
def test_limpiar_que_no_deja_una_copia_limpia_lanza(ffmpeg, tmp_path, rc, escribe, mensaje):
    ffmpeg.rc, ffmpeg.escribe = rc, escribe
    with pytest.raises(RuntimeError) as e:
        worker.limpiar_etiquetas(archivo(tmp_path, ID3V2 + AUDIO), ".mp3")
    assert str(e.value) == mensaje


# ----------------------------------------------------------------------------- la plataforma (doble)
class R:
    def __init__(self, status=200, js=None, content=b""):
        self.status_code, self._js, self.content = status, js, content

    def json(self):
        return self._js

    def raise_for_status(self):
        if self.status_code >= 400:
            raise worker.requests.HTTPError(
                f"{self.status_code} Server Error for url: http://subir/x?token=eyJhbGciOiJIUzI1NiJ9.secreto.firma")


class Plataforma:
    """stream-limpiar, la descarga y la subida. `estado` fija el HTTP de cada llamada."""

    def __init__(self, monkeypatch, contenido=AUDIO, ruta="dj/streams/x.mp3", trabajo=True, **estado):
        self.contenido, self.ruta, self.trabajo, self.estado = contenido, ruta, trabajo, estado
        self.llamadas, self.cabeceras, self.subidas, self.temporales = [], [], [], []
        monkeypatch.setattr(worker, "LIMPIEZA_DISPONIBLE", True)
        for m in ("post", "get", "put"):
            monkeypatch.setattr(worker.requests, m, getattr(self, m))
        real = worker.tempfile.mkdtemp
        monkeypatch.setattr(worker.tempfile, "mkdtemp",
                            lambda **kw: self.temporales.append(real(**kw)) or self.temporales[-1])

    def post(self, url, headers=None, json=None, timeout=None):
        base, accion = url.split("?action=")
        assert base == f"{worker.WORKER_API_URL}/stream-limpiar"
        self.llamadas.append((accion, json))
        self.cabeceras.append(headers)
        if accion in self.estado:
            return R(self.estado[accion])
        if accion != "next":
            return R(200, {"ok": True})
        if not self.trabajo:
            return R(200, {"job": None})
        return R(200, {"job": {"id": "j1", "track_id": "t1"}, "audio_url": "http://audio/x?token=abc",
                       "upload": {"url": "http://subir/x?token=abc", "path": self.ruta}})

    def get(self, url, timeout=None):
        assert url == "http://audio/x?token=abc"
        return R(self.estado.get("get", 200), content=self.contenido)

    def put(self, url, data=None, headers=None, timeout=None):
        self.subidas.append((url, data.read(), headers))
        return R(self.estado.get("put", 200))

    def acciones(self):
        return [a for a, _ in self.llamadas]


@pytest.fixture
def limpia_de_mentira(monkeypatch):
    """limpiar_etiquetas sin ffmpeg: deja el audio pelado al lado, como la de verdad."""
    hechas = []

    def limpiar(src, ext):
        hechas.append((src, ext))
        out = src + ".limpio" + ext
        with open(out, "wb") as f:
            f.write(AUDIO)
        return out

    monkeypatch.setattr(worker, "limpiar_etiquetas", limpiar)
    return hechas


# ----------------------------------------------------------------------------- _limpiar_api
def test_la_api_manda_el_secreto_del_worker(monkeypatch):
    plat = Plataforma(monkeypatch)
    assert worker._limpiar_api("result", {"job_id": "j1"}) == {"ok": True}
    assert plat.llamadas == [("result", {"job_id": "j1"})]
    assert plat.cabeceras[0]["x-worker-secret"] == worker.WORKER_SECRET


def test_la_api_sin_cuerpo_manda_un_objeto_vacio(monkeypatch):
    plat = Plataforma(monkeypatch, trabajo=False)
    assert worker._limpiar_api("next") == {"job": None}
    assert plat.llamadas == [("next", {})]


def test_404_solo_se_perdona_al_pedir_trabajo(monkeypatch):
    Plataforma(monkeypatch, next=404, result=404)
    assert worker._limpiar_api("next") is None             # la función no está desplegada todavía
    with pytest.raises(worker.requests.HTTPError):
        worker._limpiar_api("result", {})                  # al reportar, un 404 sí es una falla


def test_otro_error_al_pedir_trabajo_no_se_confunde_con_que_no_existe(monkeypatch):
    Plataforma(monkeypatch, next=500)
    with pytest.raises(worker.requests.HTTPError):
        worker._limpiar_api("next")


# ----------------------------------------------------------------------------- poll_limpiar_streams
def test_apagada_no_consulta_la_plataforma(monkeypatch):
    plat = Plataforma(monkeypatch)
    monkeypatch.setattr(worker, "LIMPIEZA_DISPONIBLE", False)
    assert worker.poll_limpiar_streams() is False
    assert plat.llamadas == []


def test_un_error_pasajero_no_apaga_la_limpieza(monkeypatch, capsys):
    plat = Plataforma(monkeypatch, next=500)
    assert worker.poll_limpiar_streams() is False
    assert worker.LIMPIEZA_DISPONIBLE is True              # a diferencia del 404, aquí se vuelve a intentar
    assert worker.poll_limpiar_streams() is False
    assert plat.acciones() == ["next", "next"]
    salida = capsys.readouterr().out
    assert salida.count("[limpiar-stream] no disponible: HTTPError") == 2
    assert "token" not in salida and "eyJ" not in salida   # solo el tipo del error, nunca la URL


def test_cola_vacia_no_hace_nada(monkeypatch):
    plat = Plataforma(monkeypatch, trabajo=False)
    assert worker.poll_limpiar_streams() is False
    assert plat.acciones() == ["next"] and plat.temporales == []


def test_copia_que_ya_estaba_limpia_no_se_vuelve_a_subir(monkeypatch, limpia_de_mentira):
    plat = Plataforma(monkeypatch, contenido=AUDIO)
    assert worker.poll_limpiar_streams() is True
    assert limpia_de_mentira == [] and plat.subidas == []
    assert plat.llamadas[-1] == ("result", {"job_id": "j1", "path": "dj/streams/x.mp3", "bytes": len(AUDIO)})


def test_copia_con_etiquetas_se_limpia_y_se_sube_encima(monkeypatch, limpia_de_mentira):
    plat = Plataforma(monkeypatch, contenido=ID3V2 + AUDIO + ID3V1)
    assert worker.poll_limpiar_streams() is True
    (url, datos, cabeceras), = plat.subidas
    assert url == "http://subir/x?token=abc" and datos == AUDIO
    assert cabeceras == {"Content-Type": "audio/mpeg", "x-upsert": "true"}   # misma ruta: reemplaza
    assert plat.llamadas[-1] == ("result", {"job_id": "j1", "path": "dj/streams/x.mp3", "bytes": len(AUDIO)})
    assert plat.acciones() == ["next", "result"]


@pytest.mark.parametrize("ruta, ext, tipo", [
    ("dj/streams/x.mp3", ".mp3", "audio/mpeg"),
    ("dj/streams/x.m4a", ".m4a", "audio/mp4"),
    ("dj/streams/x.MP4", ".m4a", "audio/mp4"),
    ("dj/streams/x.aac", ".m4a", "audio/mp4"),
    ("dj/streams/x", ".mp3", "audio/mpeg"),                # sin extensión se trata como MP3
])
def test_el_formato_sale_de_la_ruta_de_subida(monkeypatch, limpia_de_mentira, ruta, ext, tipo):
    plat = Plataforma(monkeypatch, contenido=ID3V2 + AUDIO, ruta=ruta)
    assert worker.poll_limpiar_streams() is True
    (src, ext_usada), = limpia_de_mentira
    assert ext_usada == ext and src.endswith("in" + ext)
    assert plat.subidas[0][2]["Content-Type"] == tipo
    assert plat.llamadas[-1][1]["path"] == ruta            # se reporta la ruta tal cual llegó


def test_respuesta_sin_datos_de_subida_no_tumba_el_worker(monkeypatch, limpia_de_mentira):
    plat = Plataforma(monkeypatch, contenido=ID3V2 + AUDIO)
    real = plat.post

    def post(url, **kw):
        r = real(url, **kw)
        if url.endswith("next"):
            r._js.pop("upload")
        return r

    monkeypatch.setattr(worker.requests, "post", post)
    assert worker.poll_limpiar_streams() is True
    assert plat.subidas == [] and plat.acciones() == ["next", "fail"]


# ----------------------------------------------------------------------------- fallas: se reportan y se sigue
def test_descarga_que_falla_se_reporta_y_no_se_sube_nada(monkeypatch, limpia_de_mentira, capsys):
    plat = Plataforma(monkeypatch, get=403)
    assert worker.poll_limpiar_streams() is True            # tomó un trabajo: el bucle no duerme
    assert plat.subidas == [] and limpia_de_mentira == []
    assert plat.llamadas[-1] == ("fail", {"job_id": "j1", "error": "descarga fallida (HTTP 403)"})
    assert "[limpiar-stream] t1: fallo (descarga fallida (HTTP 403))" in capsys.readouterr().out


def test_ffmpeg_que_falla_se_reporta_y_la_copia_vieja_queda_como_estaba(monkeypatch):
    plat = Plataforma(monkeypatch, contenido=ID3V2 + AUDIO)

    def limpiar(src, ext):
        raise RuntimeError("ffmpeg no pudo limpiar (1)")

    monkeypatch.setattr(worker, "limpiar_etiquetas", limpiar)
    assert worker.poll_limpiar_streams() is True
    assert plat.subidas == []                               # no se pisa la copia con algo roto
    assert plat.llamadas[-1] == ("fail", {"job_id": "j1", "error": "ffmpeg no pudo limpiar (1)"})


def test_subida_que_falla_se_reporta(monkeypatch, limpia_de_mentira):
    plat = Plataforma(monkeypatch, contenido=ID3V2 + AUDIO, put=500)
    assert worker.poll_limpiar_streams() is True
    assert plat.acciones() == ["next", "fail"]              # no dice "result" de algo que no subió
    assert plat.llamadas[-1][1] == {"job_id": "j1", "error": "subida fallida (HTTP 500)"}


def test_si_guardar_el_resultado_falla_el_error_va_sin_la_firma(monkeypatch, limpia_de_mentira, capsys):
    plat = Plataforma(monkeypatch, contenido=ID3V2 + AUDIO, result=500)
    assert worker.poll_limpiar_streams() is True
    assert plat.acciones() == ["next", "result", "fail"]
    error = plat.llamadas[-1][1]["error"]
    assert "token=***" in error and "eyJ" not in error and "secreto" not in error
    salida = capsys.readouterr().out
    assert "token=***" in salida and "eyJ" not in salida


def test_el_error_que_se_guarda_tiene_tope_de_500_caracteres(monkeypatch):
    plat = Plataforma(monkeypatch, contenido=ID3V2 + AUDIO)

    def limpiar(src, ext):
        raise RuntimeError("x" * 2000)

    monkeypatch.setattr(worker, "limpiar_etiquetas", limpiar)
    assert worker.poll_limpiar_streams() is True
    assert plat.llamadas[-1][1]["error"] == "x" * 500


def test_si_ni_siquiera_se_puede_reportar_la_falla_el_worker_sigue(monkeypatch, limpia_de_mentira):
    plat = Plataforma(monkeypatch, get=500, fail=500)
    assert worker.poll_limpiar_streams() is True            # no lanza
    assert plat.acciones() == ["next", "fail"]


# ----------------------------------------------------------------------------- temporales
@pytest.mark.parametrize("estado", [{}, {"get": 500}, {"put": 500}, {"result": 500}, {"fail": 500, "get": 404}],
                         ids=["todo bien", "sin descarga", "sin subida", "sin resultado", "sin reporte"])
def test_el_temporal_se_borra_pase_lo_que_pase(monkeypatch, limpia_de_mentira, estado):
    plat = Plataforma(monkeypatch, contenido=ID3V2 + AUDIO, **estado)
    assert worker.poll_limpiar_streams() is True
    carpeta, = plat.temporales
    assert os.path.basename(carpeta).startswith("dm_limpiar_")
    assert not os.path.exists(carpeta)                      # ni el original ni la copia limpia quedan en disco
