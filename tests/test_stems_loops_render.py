"""Loops y render de stems_worker: lo que test_stems_puras y test_stems_procesos no cubrían
(noche 11→12-oct-2026). Sin cambiar código.

- Qué rol recibe cada loop que no es de bajo ni de batería: «atmosfera» (other en un tramo
  flojo), «melodia» (la pista armónica con más notas, si tiene 2 o más por compás) o «acordes».
- Cómo se cuentan los rechazos (sin el detalle entre paréntesis) y de dónde sale el largo del
  tema cuando la cola no lo manda.
- El contrato con la plataforma: loops-next, loops-result, render-next, render-result y la
  subida del render (sin firmas y con el error recortado)."""
import os
import shutil
import sys

import numpy as np
import pytest
import requests

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stems_worker as sw  # noqa: E402

SR = 22050
BPM = 120
DUR = 70.0  # 35 compases a 120: alcanzan las ventanas 1 y 17
con_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="sin ffmpeg")


def tono(hz=220, dur=DUR, amp=0.3):
    """Sostenido: pasa el control. A 220, 330 y 440 Hz tiene 0 notas por compás; a otras
    alturas el detector ve notas falsas (ver test_un_pad_sostenido_no_deberia_tener_notas)."""
    t = np.arange(int(dur * SR)) / SR
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def notas(dur=DUR, amp=0.3):
    """Corcheas de alturas distintas: unas 8 notas por compás."""
    y = np.zeros(int(dur * SR), dtype=np.float32)
    paso, alturas, t, k = 60 / BPM / 2, (220, 277, 330, 440), 0.0, 0
    while t < dur:
        i = int(t * SR)
        n = min(int(paso * SR), len(y) - i)
        tt = np.arange(n) / SR
        y[i:i + n] += amp * np.sin(2 * np.pi * alturas[k % 4] * tt) * np.exp(-tt * 6)
        t, k = t + paso, k + 1
    return y


def cortar(audio, energia=None, bars=32, calidad=None):
    track = {"bpm": BPM, "phase_ms": 0, "bars": bars}
    lanes = {s: [{"from": 1, "to": 32}] for s in audio}
    calidad = calidad or {s: {"estado": "limpia"} for s in audio}
    energia = energia if energia is not None else {"drums": [0.5] * 32, "bass": [0.5] * 32}
    return sw.cortar_loops(track, lanes, energia, calidad, {s: (y, SR) for s, y in audio.items()})


def roles(loops):
    return {(lp["stem"], lp["from_bar"]): lp["role"] for lp in loops}


# ── el rol de cada loop ──────────────────────────────────────────────────────

def test_other_en_tramo_flojo_es_atmosfera():
    """La energía de batería + bajo en los compases 1–8 es menos de la mitad de la mediana:
    el «other» de ahí es atmósfera; el de 17–24, con energía normal, no."""
    nivel = [0.1] * 8 + [1.0] * 24
    loops, _ = cortar({"other": tono()}, energia={"drums": nivel, "bass": [0.0] * 32})
    assert roles(loops) == {("other", 1): "atmosfera", ("other", 17): "acordes"}


def test_la_pista_con_notas_es_melodia_y_la_sostenida_acordes():
    loops, rechazos = cortar({"piano": notas(), "guitar": tono(330)})
    assert roles(loops) == {("piano", 1): "melodia", ("piano", 17): "melodia",
                            ("guitar", 1): "acordes", ("guitar", 17): "acordes"}
    assert rechazos == {}
    for lp in loops:  # tonalidad medida en el propio loop (v1.20–1.21), sin fase medida (no hay batería)
        assert {"afinacion_cents", "key_detectada", "key_conf"} <= set(lp["qc"])
        assert lp["qc"]["fase_medida_ms"] is None and lp["qc"]["fase_dada_ms"] == 0


def test_la_melodia_necesita_dos_notas_por_compas():
    """Si la única pista armónica es sostenida, gana la elección de melodía pero no llega a 2
    notas por compás: queda como acordes."""
    loops, _ = cortar({"piano": tono(220)})
    assert set(roles(loops).values()) == {"acordes"}


@pytest.mark.xfail(strict=True, reason="onsets_s no tiene piso absoluto: el vaivén del espectro de un sonido "
                                       "sostenido supera mediana + 2,5 desvíos y cuenta como notas")
@pytest.mark.parametrize("senal", ["tono_262", "pad"])
def test_un_pad_sostenido_no_deberia_tener_notas(senal):
    """FALLA CONOCIDA (noche 11→12-oct, avisada en #30): un tono puro de 262 Hz da ~43 «notas»
    por compás y un pad (do mayor con armónicos y vibrato) ~30. Con eso, el «other» de un pad
    con energía normal sale como «melodia» y no como «acordes». Cuando se arregle el detector,
    esta prueba pasa y el xfail estricto avisa para quitar la marca."""
    t = np.arange(int(20 * SR)) / SR
    if senal == "tono_262":
        y = tono(262, dur=20)
    else:
        y = sum(.1 / k * np.sin(2 * np.pi * f * k * t + 0.002 * k * np.sin(2 * np.pi * 5 * t))
                for f in (130.8, 164.8, 196.0) for k in (1, 2, 3)).astype(np.float32)
    assert sw.densidad_notas(y, SR, BPM, 0, 1) < 2


def test_una_pista_no_limpia_no_compite_por_la_melodia():
    """El piano con notas está «mezclado»: no se corta ni se elige como melodía; la guitarra
    sostenida queda como acordes."""
    loops, rechazos = cortar({"piano": notas(), "guitar": tono(330)},
                             calidad={"piano": {"estado": "mezclada"}, "guitar": {"estado": "con_algo"}})
    assert roles(loops) == {("guitar", 1): "acordes", ("guitar", 17): "acordes"}
    assert rechazos == {"pista no limpia": 1}


# ── rechazos y largo del tema ────────────────────────────────────────────────

def test_los_rechazos_se_cuentan_sin_el_detalle():
    y = tono(55)
    hueco = y.copy()
    hueco[int(2 * SR):int(6 * SR)] = 0  # compases 2 y 3 en silencio: la ventana 1 se rechaza
    desparejo = tono(330)
    fin = sw.muestra_de_compas(25, BPM, 0, SR)
    desparejo[fin:] *= 0.2               # al repetirse, el loop 17–24 no empalma con lo que sigue
    loops, rechazos = cortar({"bass": hueco, "other": desparejo})
    # Una ventana rechazada no aparta a las vecinas: la separación de 16 compases se mide solo
    # contra las aceptadas (bajo: 9 y 25; other: 1 y 25).
    assert roles(loops) == {("bass", 9): "bajo", ("bass", 25): "bajo",
                            ("other", 1): "acordes", ("other", 25): "acordes"}
    assert rechazos == {"hueco: la pista se apaga más de un compás": 1, "empalme desparejo": 1}


def test_sin_bars_el_largo_sale_de_las_pistas():
    """La cola no mandó `bars`: el largo es el mayor «to» de los carriles (o el largo de la energía)."""
    track = {"bpm": BPM, "phase_ms": 0}
    loops, _ = sw.cortar_loops(track, {"bass": [{"from": 1, "to": 16}]}, {}, {"bass": {"estado": "limpia"}},
                               {"bass": (tono(55), SR)})
    assert [lp["from_bar"] for lp in loops] == [1]   # con 16 compases solo cabe la ventana 1
    loops, _ = sw.cortar_loops(track, {"bass": None}, {"drums": [0.5] * 24}, {"bass": {"estado": "limpia"}},
                               {"bass": (tono(55), SR)})
    assert loops == []                                # sin carril, ninguna ventana es candidata


# ── contrato con la plataforma ───────────────────────────────────────────────

class Resp:
    def __init__(self, status=200, datos=None):
        self.status_code, self._datos = status, datos or {}

    def json(self):
        return self._datos

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


@pytest.fixture
def red(monkeypatch):
    estado = {"posts": [], "puts": [], "respuesta": Resp(200, {})}

    def post(url, headers=None, json=None, timeout=None):
        estado["posts"].append((url, headers, json))
        return estado["respuesta"]

    def put(url, data=None, headers=None, timeout=None):
        estado["puts"].append((url, data.read(), headers))
        return estado["respuesta"]

    monkeypatch.setattr(sw.requests, "post", post)
    monkeypatch.setattr(sw.requests, "put", put)
    return estado


@pytest.mark.parametrize("pedir,ruta", [(sw.claim_loops, "/loops-next"), (sw.claim_render, "/render-next")])
def test_pedir_trabajo(red, pedir, ruta):
    red["respuesta"] = Resp(200, {"job": None})
    assert pedir() is None
    red["respuesta"] = Resp(200, {"job": {"id": "J"}, "track": {}})
    assert pedir() == {"job": {"id": "J"}, "track": {}}
    url, headers, _ = red["posts"][-1]
    assert url == sw.API + ruta and headers["x-worker-secret"] == sw.SECRET
    red["respuesta"] = Resp(500)
    with pytest.raises(requests.HTTPError):
        pedir()


def test_report_loops(red):
    red["respuesta"] = Resp(200, {"guardados": 2})
    assert sw.report_loops("J", True, [{"stem": "bass"}]) == {"guardados": 2}
    assert red["posts"][-1][2] == {"job_id": "J", "ok": True, "loops": [{"stem": "bass"}]}
    sw.report_loops("J", False, error="x" * 900)
    cuerpo = red["posts"][-1][2]
    assert cuerpo["loops"] == [] and len(cuerpo["error"]) == 500
    red["respuesta"] = Resp(502)
    with pytest.raises(requests.HTTPError):
        sw.report_loops("J", True)


def test_report_render(red):
    sw.report_render("J", True, {"lufs": -9.1})
    assert red["posts"][-1][0] == sw.API + "/render-result"
    assert red["posts"][-1][2] == {"job_id": "J", "ok": True, "medidas": {"lufs": -9.1}}
    sw.report_render("J", True)
    assert red["posts"][-1][2] == {"job_id": "J", "ok": True}   # sin medidas no manda la clave
    sw.report_render("J", False, error="y" * 2000)
    assert len(red["posts"][-1][2]["error"]) == 900
    red["respuesta"] = Resp(500)
    with pytest.raises(requests.HTTPError):
        sw.report_render("J", True)


def test_subir_el_render(red, tmp_path):
    p = tmp_path / "tema.mp3"
    p.write_bytes(b"audio")
    sw.subir("http://firmada", p, "audio/mpeg")
    url, datos, headers = red["puts"][-1]
    assert (url, datos) == ("http://firmada", b"audio")
    assert headers == {"Content-Type": "audio/mpeg", "x-upsert": "true"}
    red["respuesta"] = Resp(403)
    with pytest.raises(requests.HTTPError):
        sw.subir("http://firmada", p, "audio/mpeg")


@con_ffmpeg
@pytest.mark.parametrize("formato,ext", [("wav", ".wav"), ("mp3", ".mp3")])
def test_codificar_y_volver_a_leer(tmp_path, formato, ext):
    """El render codificado (WAV 24 bits o MP3 320k) se vuelve a leer estéreo y con el mismo nivel."""
    t = np.arange(SR * 2) / sw.SR_RENDER
    estereo = np.stack([0.5 * np.sin(2 * np.pi * 440 * t), 0.25 * np.sin(2 * np.pi * 440 * t)]).astype(np.float32)
    dest = tmp_path / f"render{ext}"
    sw.codificar(estereo, dest, formato)
    leido = sw.to_wav_estereo(dest)
    assert leido.shape[0] == 2
    medio = slice(sw.SR_RENDER // 4, sw.SR_RENDER)
    pico = np.abs(leido[:, medio]).max(axis=1)
    assert pico[0] == pytest.approx(0.5, abs=0.02) and pico[1] == pytest.approx(0.25, abs=0.02)
