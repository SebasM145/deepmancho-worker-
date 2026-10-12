"""Parecido v0: los casos borde que tests/test_parecido.py no cubría (noche 11→12-oct-2026).

Sin cambiar código. Cubre:
- La cola del banco de calibración (`parecido-next`): la red caída no apaga la cola, el 404 solo
  la apaga en «next», un error que no es del archivo no se marca «determinista», si no se puede
  guardar el resultado el worker sigue, y el temporal se borra.
- La semilla sin huella vigente: se baja, se mide con su tempo, ancla y tonalidad, se borra el
  temporal y la huella nueva viaja en la respuesta. Sin huella ni audio, falla con un mensaje claro.
- `huella_completa` de una toma: silencio → AudioMudo; tempo imposible o detector caído → sin BPM.
- `parecido.huella_de_archivo` y la energía de una toma sin curva (cuenta 0,5)."""
import os
import sys

import numpy as np
import pytest
import requests

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import parecido  # noqa: E402
import worker  # noqa: E402

SR = 11025


def pulso(bpm=124, dur=20, raiz=220.0, semilla=1):
    """Bombo en negras con un acorde de fondo: lo justo para una huella con forma."""
    rng = np.random.default_rng(semilla)
    t = np.arange(int(SR * dur)) / SR
    y = 0.08 * (np.sin(2 * np.pi * raiz * t) + np.sin(2 * np.pi * raiz * 1.5 * t))
    for k in np.arange(0.25, dur, 60 / bpm):
        i = int(k * SR)
        n = min(int(.12 * SR), len(y) - i)
        tt = np.arange(n) / SR
        y[i:i + n] += .9 * np.sin(2 * np.pi * (50 + 80 * np.exp(-tt * 30)) * tt) * np.exp(-tt * 18)
    y += 0.01 * rng.standard_normal(len(y))
    return y.astype(np.float32)


@pytest.fixture(scope="module")
def huellas():
    return {
        "semilla": parecido.huella(pulso(124), SR, 124, 250, "8A"),
        "toma": parecido.huella(pulso(125, semilla=2), SR, 125, 250, "8A"),
        "base": parecido.huella(pulso(128, raiz=196, semilla=3), SR, 128, 250, "6A"),
    }


class Resp:
    def __init__(self, status=200, datos=None):
        self.status_code, self._datos = status, datos or {}

    def json(self):
        return self._datos

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


# ── _parecido_api ────────────────────────────────────────────────────────────

def test_api_manda_secreto_y_capacidad(monkeypatch):
    vistas = []

    def post(url, headers=None, json=None, timeout=None):
        vistas.append((url, headers, json))
        return Resp(200, {"calibracion": None})

    monkeypatch.setattr(worker.requests, "post", post)
    assert worker._parecido_api("next") == {"calibracion": None}
    url, headers, cuerpo = vistas[0]
    assert url.endswith("/parecido-next?action=next")
    assert headers["x-worker-secret"] == worker.WORKER_SECRET
    assert headers["x-worker-capacidades"] == worker.CAPACIDADES
    assert cuerpo == {}


def test_api_404_solo_es_hueco_en_next(monkeypatch):
    """El 404 de «next» es «la función no existe todavía»; en «resultado» es un error de verdad."""
    monkeypatch.setattr(worker.requests, "post", lambda *a, **k: Resp(404))
    assert worker._parecido_api("next") is None
    with pytest.raises(requests.HTTPError):
        worker._parecido_api("resultado", {"calibracion_id": "C"})


# ── poll_parecido_calibracion ────────────────────────────────────────────────

@pytest.fixture
def cola(monkeypatch, tmp_path, huellas):
    monkeypatch.setattr(worker, "CALIBRACION_DISPONIBLE", True)
    estado = {"respuestas": [], "llamadas": [], "temporales": [], "falla_guardar": False}

    def api(action, payload=None):
        estado["llamadas"].append((action, payload))
        if action == "next":
            r = estado["respuestas"].pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        if estado["falla_guardar"]:
            raise requests.ConnectionError("sin red")
        return {}

    def download_audio(url):
        p = tmp_path / f"cal{len(estado['temporales'])}.wav"
        p.write_bytes(b"x")
        estado["temporales"].append(str(p))
        return str(p)

    monkeypatch.setattr(worker, "_parecido_api", api)
    monkeypatch.setattr(worker, "download_audio", download_audio)
    monkeypatch.setattr(worker, "huella_completa", lambda p: huellas["toma"])
    return estado


def pedido(huellas, **extra):
    return {"calibracion": {"id": "C1", "audio_url": "http://cal"},
            "parecido": {"semilla_track_id": "S", "semilla_rasgos": huellas["semilla"],
                         "bases": [{"track_id": "b1", "rasgos": huellas["base"]}], **extra}}


def test_red_caida_no_apaga_la_cola(cola):
    cola["respuestas"] = [requests.ConnectionError("sin red"), {"calibracion": None}]
    assert worker.poll_parecido_calibracion() is False
    assert worker.CALIBRACION_DISPONIBLE is True
    worker.poll_parecido_calibracion()
    assert len(cola["llamadas"]) == 2  # vuelve a preguntar en la siguiente vuelta


def test_el_temporal_se_borra_al_terminar(cola, huellas):
    cola["respuestas"] = [pedido(huellas)]
    assert worker.poll_parecido_calibracion() is True
    assert cola["temporales"] and not any(os.path.exists(p) for p in cola["temporales"])
    accion, payload = cola["llamadas"][-1]
    assert accion == "resultado" and payload["error"] is None
    assert payload["parecido"]["semilla_track_id"] == "S"
    assert "semilla_rasgos" not in payload["parecido"]  # la semilla ya tenía huella: no se reenvía


def test_error_que_no_es_del_archivo_no_es_determinista(cola, huellas, monkeypatch):
    """Solo AudioMudo, ArchivoIlegible y ArchivoMuyGrande se marcan «determinista:» (no se reintentan)."""
    def caido(p):
        raise ValueError("librosa se cayó")
    monkeypatch.setattr(worker, "huella_completa", caido)
    cola["respuestas"] = [pedido(huellas)]
    assert worker.poll_parecido_calibracion() is True
    _, payload = cola["llamadas"][-1]
    assert payload["parecido"] is None and payload["error"] == "librosa se cayó"
    assert not any(os.path.exists(p) for p in cola["temporales"])


def test_archivo_muy_grande_es_determinista(cola, huellas, monkeypatch):
    def grande(url):
        raise worker.ArchivoMuyGrande("el archivo pesa 900 MB")
    monkeypatch.setattr(worker, "download_audio", grande)
    cola["respuestas"] = [pedido(huellas)]
    assert worker.poll_parecido_calibracion() is True
    _, payload = cola["llamadas"][-1]
    assert payload["error"] == "determinista:el archivo pesa 900 MB"


def test_toma_sin_huella_valida_se_reporta(cola, huellas, monkeypatch):
    monkeypatch.setattr(worker, "huella_completa", lambda p: {"v": parecido.VERSION, "mfcc_media": []})
    cola["respuestas"] = [pedido(huellas)]
    worker.poll_parecido_calibracion()
    _, payload = cola["llamadas"][-1]
    assert payload["parecido"] is None and payload["error"] == "la toma no tiene huella"


def test_si_no_se_puede_guardar_el_worker_sigue(cola, huellas):
    cola["respuestas"] = [pedido(huellas)]
    cola["falla_guardar"] = True
    assert worker.poll_parecido_calibracion() is True  # no lanza
    assert cola["llamadas"][-1][0] == "resultado"


# ── la semilla sin huella vigente ────────────────────────────────────────────

def test_semilla_sin_huella_ni_audio_falla_claro():
    with pytest.raises(RuntimeError, match="la semilla no tiene huella ni audio"):
        worker.huella_de_semilla("J", {"semilla_rasgos": None})


def test_semilla_con_huella_vieja_se_baja_y_se_mide(monkeypatch, tmp_path, huellas):
    """Una huella de otra versión no vale: se baja la semilla, se mide con SU tempo, ancla y
    tonalidad, se borra el temporal y la huella nueva vuelve para guardarse."""
    temporal = tmp_path / "semilla.wav"
    temporal.write_bytes(b"x")
    vistos = []

    def huella_de_archivo(path, bpm, ancla, key, sr, dur):
        vistos.append((path, bpm, ancla, key, sr, dur))
        return huellas["semilla"]

    monkeypatch.setattr(worker, "download_audio", lambda url: str(temporal))
    monkeypatch.setattr(parecido, "huella_de_archivo", huella_de_archivo)
    vieja = {**huellas["semilla"], "v": parecido.VERSION + 99}
    p = {"semilla_track_id": "S", "semilla_rasgos": vieja, "semilla_audio_url": "http://semilla",
         "semilla": {"bpm": 124, "first_beat_ms": 250, "key": "8A"},
         "bases": [{"track_id": "b1", "rasgos": huellas["base"]},
                   {"track_id": "rota", "rasgos": {"v": parecido.VERSION}},  # sin MFCC: se descarta
                   {"track_id": "nula", "rasgos": None}]}
    out = worker.medir_parecido_toma("J", huellas["toma"], p)
    assert vistos == [(str(temporal), 124, 250, "8A", worker.SR, worker.MAX_DURATION)]
    assert not temporal.exists()
    assert out["semilla_rasgos"] == huellas["semilla"]
    assert [t["track_id"] for t in out["linea_base"]["temas"]] == ["b1"]


def test_si_medir_la_semilla_falla_el_temporal_igual_se_borra(monkeypatch, tmp_path):
    temporal = tmp_path / "semilla.wav"
    temporal.write_bytes(b"x")
    monkeypatch.setattr(worker, "download_audio", lambda url: str(temporal))
    monkeypatch.setattr(parecido, "huella_de_archivo", lambda *a: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        worker.huella_de_semilla("J", {"semilla_audio_url": "http://semilla"})
    assert not temporal.exists()


# ── huella_completa (toma del banco, sin analyze) ────────────────────────────

def escribir(tmp_path, y, nombre="toma.wav"):
    import soundfile as sf
    p = tmp_path / nombre
    sf.write(p, y, SR)
    return str(p)


def test_toma_en_silencio_es_audio_mudo(tmp_path):
    with pytest.raises(worker.AudioMudo):
        worker.huella_completa(escribir(tmp_path, np.zeros(SR * 5, dtype=np.float32)))


@pytest.mark.parametrize("rejilla", ["cae", 300.0, 30.0])
def test_tempo_imposible_o_detector_caido_queda_sin_bpm(tmp_path, monkeypatch, rejilla):
    def detect_grid(y, sr, seed_bpm=None):
        if rejilla == "cae":
            raise RuntimeError("sin pulso")
        return rejilla, 100
    monkeypatch.setattr(worker, "detect_grid", detect_grid)
    hh = worker.huella_completa(escribir(tmp_path, pulso(124)))
    assert parecido.huella_valida(hh)
    assert hh["bpm"] is None
    assert hh["energia"] is not None  # sin rejilla, la energía va en ventanas de 2 s


# ── parecido.py ──────────────────────────────────────────────────────────────

def test_huella_de_archivo_mide_con_lo_que_recibe(tmp_path):
    hh = parecido.huella_de_archivo(escribir(tmp_path, pulso(124)), 124, 250, "8A")
    assert parecido.huella_valida(hh)
    assert hh["bpm"] == 124 and hh["tonalidad"] == "8A" and hh["sr"] == SR


def test_huella_de_archivo_vacio_falla(tmp_path):
    with pytest.raises(ValueError, match="audio vacío"):
        parecido.huella_de_archivo(escribir(tmp_path, np.zeros(0, dtype=np.float32)))


def test_toma_sin_curva_de_energia_cuenta_medio(huellas):
    """La semilla tiene curva y la toma no (demasiado corta): es diferencia, 0,5, no un hueco."""
    toma = {**huellas["toma"], "energia": None}
    _, comp = parecido.puntaje_bruto(toma, huellas["semilla"])
    assert comp["energia"] == 0.5
    assert "energia" not in comp["faltan"]


def test_semilla_con_curva_plana_si_es_hueco(huellas):
    """Una curva plana no tiene forma: no se puede comparar y su peso se reparte."""
    semilla = {**huellas["semilla"], "energia": [0.3] * parecido.PUNTOS_ENERGIA}
    _, comp = parecido.puntaje_bruto({**huellas["toma"], "energia": None}, semilla)
    assert comp["energia"] is None and "energia" in comp["faltan"]
