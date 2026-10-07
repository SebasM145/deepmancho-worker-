"""Parecido v0 entre una toma generada y su tema semilla (6-oct-2026, pedido del Estudio).

Tres partes:
- La cuenta (parecido.py, funciones puras): cada componente y la línea base.
- Audio sintético con resultado conocido: la toma igual a la semilla da 100, una casi igual
  queda alta, un tema ambiental sin pulso queda en 0.
- El contrato con la plataforma: `analyze` agrega `rasgos`, `process_job` manda `parecido`
  solo si worker-next lo pidió (y si falla, el análisis sigue), y la cola del banco de
  calibración (`parecido-next`)."""
import os
import sys

import numpy as np
import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import parecido  # noqa: E402
import worker  # noqa: E402

SR = 11025


def club(bpm, raiz=220.0, arco=(1, 1, .2, 1), dur=64, semilla=1, hat=0.2, acorde=(1, 1.19, 1.5)):
    """Bombo en negras, hat en el contratiempo y un acorde; `arco` da la forma de la energía."""
    rng = np.random.default_rng(semilla)
    t = np.arange(int(SR * dur)) / SR
    y = np.zeros_like(t)
    b = 60 / bpm
    for k in np.arange(0.25, dur, b):
        i = int(k * SR)
        n = min(int(.12 * SR), len(y) - i)
        tt = np.arange(n) / SR
        g = arco[min(int(k / dur * len(arco)), len(arco) - 1)]
        y[i:i + n] += g * .9 * np.sin(2 * np.pi * (50 + 80 * np.exp(-tt * 30)) * tt) * np.exp(-tt * 18)
        j = int((k + b / 2) * SR)
        m = min(int(.02 * SR), max(0, len(y) - j))
        y[j:j + m] += g * hat * rng.standard_normal(m)
    for f in acorde:
        y += .08 * np.sin(2 * np.pi * raiz * f * t)
    return y.astype(np.float32)


def ambiente(dur=64, semilla=7):
    """Colchón de acordes que crece, sin pulso."""
    rng = np.random.default_rng(semilla)
    t = np.arange(int(SR * dur)) / SR
    env = np.linspace(.2, 1, len(t))
    y = sum(.15 * np.sin(2 * np.pi * f * t + rng.uniform(0, 6)) for f in (146.8, 174.6, 220, 293.7)) * env
    y += .03 * np.convolve(rng.standard_normal(len(t)), np.ones(200) / 200, "same")
    return y.astype(np.float32)


def h(y, bpm, key):
    return parecido.huella(y, SR, bpm, 250, key)


@pytest.fixture(scope="module")
def huellas():
    return {
        "semilla": h(club(124), 124, "8A"),
        "igual": h(club(124, semilla=5), 124, "8A"),
        "cerca": h(club(125.5, semilla=2, hat=.25), 125.5, "8A"),
        "ambiente": h(ambiente(), None, "10B"),
        "bases": [("b1", h(club(122, raiz=196, arco=(.3, 1, 1, 1), semilla=3, hat=.5), 122, "6A")),
                  ("b2", h(club(128, raiz=246.9, arco=(1, .2, 1, .2), semilla=4, hat=.05, acorde=(1, 1.26, 1.5)), 128, "11B")),
                  ("b3", h(ambiente(semilla=9), None, "4A"))],
    }


# ── componentes ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("toma,semilla,esperado", [
    (124, 124, 1.0), (127, 124, 0.5), (130, 124, 0.0), (140, 124, 0.0),
    (62, 124, 1.0),    # la mitad del tempo de la semilla
    (128, 64, 1.0),    # el doble
    (None, 124, None), (124, None, None),
])
def test_bpm_con_mitad_y_doble(toma, semilla, esperado):
    v = parecido.parecido_bpm(toma, semilla)
    assert v == (pytest.approx(esperado) if esperado is not None else None)


@pytest.mark.parametrize("a,b,esperado", [
    ("8A", "8A", 1.0), ("8A", "8B", 0.8), ("8A", "9A", 0.8), ("8A", "7A", 0.8),
    ("12A", "1A", 0.8),   # la rueda da la vuelta
    ("8A", "10A", 0.4), ("1B", "11B", 0.4),
    ("8A", "11A", 0.0), ("8A", "9B", 0.0), ("8A", "3A", 0.0),
    ("8a", "8A", 1.0), (None, "8A", None), ("Am", "8A", None), ("13A", "1A", None),
])
def test_tonalidad_camelot(a, b, esperado):
    assert parecido.parecido_tonalidad(a, b) == esperado


def test_energia_pearson_llevada_a_0_1():
    a = list(np.sin(np.linspace(0, 3, 32)))
    assert parecido.parecido_energia(a, a) == pytest.approx(1.0)
    assert parecido.parecido_energia(a, [-x for x in a]) == pytest.approx(0.0)
    assert parecido.parecido_energia(a, [1.0] * 32) is None   # curva plana: sin forma
    assert parecido.parecido_energia(a, a[:16]) is None


def test_timbre_sin_mfcc0_y_por_bloque(huellas):
    assert len(huellas["semilla"]["mfcc_media"]) == parecido.N_MFCC - 1
    v, detalle = parecido.parecido_timbre(huellas["semilla"], huellas["semilla"])
    assert v == pytest.approx(1.0)
    assert set(detalle) == set(parecido.PESOS_TIMBRE)


def test_un_bloque_grande_no_tapa_a_los_demas():
    """Escalar un bloque por 1000 no cambia su parecido: cada bloque se compara aparte."""
    base = {"mfcc_media": [1, 2, 3, 4], "mfcc_desvio": [1, 1, 2, 2], "contraste": [5, 6, 7], "tercios": {"a": -1, "b": -3, "c": -9}}
    otra = {"mfcc_media": [1, 2, 3, 5], "mfcc_desvio": [2, 1, 2, 1], "contraste": [5, 6, 9], "tercios": {"a": -2, "b": -3, "c": -6}}
    v1, _ = parecido.parecido_timbre(base, otra)
    grande = dict(base, mfcc_media=[x * 1000 for x in base["mfcc_media"]])
    otra_grande = dict(otra, mfcc_media=[x * 1000 for x in otra["mfcc_media"]])
    v2, _ = parecido.parecido_timbre(grande, otra_grande)
    assert v1 == pytest.approx(v2)


def test_hueco_en_la_semilla_reparte_el_peso(huellas):
    sin_bpm = dict(huellas["semilla"], bpm=None)
    bruto, comp = parecido.puntaje_bruto(huellas["igual"], sin_bpm)
    assert comp["bpm"] is None and comp["faltan"] == ["bpm"]
    assert bruto == pytest.approx(100.0, abs=0.5)


def test_hueco_en_la_toma_cuenta_como_diferencia(huellas):
    sin_pulso = dict(huellas["igual"], bpm=None, tonalidad=None)
    bruto, comp = parecido.puntaje_bruto(sin_pulso, huellas["semilla"])
    assert comp["bpm"] == 0.0 and comp["tonalidad"] == 0.0 and comp["faltan"] == []
    assert bruto <= 70.0 + 0.5


def test_sin_timbre_no_hay_puntaje():
    vacia = {"bpm": 124, "tonalidad": "8A", "energia": None}
    assert parecido.puntaje_bruto(vacia, vacia)[0] is None


# ── línea base ───────────────────────────────────────────────────────────────

def test_linea_base_formula():
    """Con base media 60 y bruto 80, el parecido es 100·(80−60)/(100−60) = 50."""
    ref = {"v": 1, "mfcc_media": [1, 2, 3, 4]}
    import unittest.mock as m
    with m.patch.object(parecido, "puntaje_bruto", side_effect=[(80.0, {}), (50.0, {}), (70.0, {})]):
        r = parecido.medir_parecido(ref, ref, [("x", ref), ("y", ref)])
    assert r["linea_base"]["media"] == 60.0
    assert r["puntaje"] == 50.0
    assert r["bruto"] == 80.0 and r["version"] == parecido.VERSION


@pytest.mark.parametrize("bruto,brutos_base,esperado", [
    (40.0, [60.0], 0.0),         # menos parecido que un tema cualquiera: 0, nunca negativo
    (100.0, [100.0], None),      # la base es igual a la semilla: no se puede medir
    (80.0, [], None),            # sin base no hay puntaje (sí bruto)
])
def test_linea_base_bordes(bruto, brutos_base, esperado):
    ref = {"v": 1, "mfcc_media": [1, 2, 3, 4]}
    import unittest.mock as m
    with m.patch.object(parecido, "puntaje_bruto", side_effect=[(bruto, {})] + [(b, {}) for b in brutos_base]):
        r = parecido.medir_parecido(ref, ref, [(str(i), ref) for i in range(len(brutos_base))])
    assert r["puntaje"] == esperado
    assert r["bruto"] == bruto


# ── audio sintético con resultado conocido ───────────────────────────────────

def test_toma_igual_a_la_semilla_da_100(huellas):
    r = parecido.medir_parecido(huellas["igual"], huellas["semilla"], huellas["bases"])
    assert r["bruto"] == pytest.approx(100.0, abs=0.5)
    assert r["puntaje"] == pytest.approx(100.0, abs=1.0)


def test_toma_casi_igual_queda_alta(huellas):
    r = parecido.medir_parecido(huellas["cerca"], huellas["semilla"], huellas["bases"])
    assert r["puntaje"] >= 75, r


def test_tema_sin_pulso_queda_en_cero(huellas):
    r = parecido.medir_parecido(huellas["ambiente"], huellas["semilla"], huellas["bases"])
    assert r["puntaje"] <= 10, r


def test_el_orden_es_el_esperado(huellas):
    p = {k: parecido.medir_parecido(huellas[k], huellas["semilla"], huellas["bases"])["puntaje"]
         for k in ("igual", "cerca", "ambiente")}
    assert p["igual"] >= p["cerca"] > p["ambiente"], p


def test_curva_de_energia_sigue_el_arco():
    """Arco alto–alto–caída–alto: el punto más bajo cae en el tercer cuarto."""
    c = parecido.curva_energia(club(124), SR, 124, 250)
    assert len(c) == parecido.PUNTOS_ENERGIA
    assert 16 <= int(np.argmin(c)) < 24
    assert parecido.curva_energia(np.zeros(SR, np.float32), SR, 124, 0) is None  # menos de 4 compases


def test_huella_es_json_y_valida(huellas):
    import json
    hs = huellas["semilla"]
    assert json.loads(json.dumps(hs)) == hs
    assert parecido.huella_valida(hs)
    assert not parecido.huella_valida(dict(hs, v=0))
    assert not parecido.huella_valida(None)


# ── contrato con la plataforma ───────────────────────────────────────────────

def test_analyze_agrega_rasgos(tmp_path):
    import soundfile as sf
    p = tmp_path / "t.wav"
    sf.write(p, club(124, dur=40), SR)
    r = worker.analyze(str(p))
    assert parecido.huella_valida(r["rasgos"])
    assert r["rasgos"]["tonalidad"] == r["key"]


def test_analyze_sigue_si_la_huella_falla(tmp_path, monkeypatch):
    import soundfile as sf
    p = tmp_path / "t.wav"
    sf.write(p, club(124, dur=40), SR)
    monkeypatch.setattr(parecido, "huella", lambda *a, **k: 1 / 0)
    r = worker.analyze(str(p))
    assert "rasgos" not in r and r["cue_points"]


@pytest.fixture
def job_simulado(monkeypatch, tmp_path, huellas):
    audio = tmp_path / "toma.mp3"
    estado = {"enviados": [], "bajadas": []}

    def download_audio(url):
        estado["bajadas"].append(url)
        audio.write_bytes(b"audio")
        return str(audio)

    monkeypatch.setattr(worker, "download_audio", download_audio)
    monkeypatch.setattr(worker, "analyze", lambda path, bpm_seed=None: {
        "bpm": 124.0, "key": "8A", "energy": 7, "cue_points": [], "rasgos": huellas["igual"]})
    monkeypatch.setattr(worker, "detectar_genero", lambda p: {})
    monkeypatch.setattr(worker, "medir_sonoridad", lambda p: {})
    monkeypatch.setattr(worker, "fingerprint_identify", lambda p: None)
    monkeypatch.setattr(worker, "ENABLE_ANCHOR_BACKFILL", False)
    monkeypatch.setattr(worker, "send_result", lambda *a, **k: estado["enviados"].append((a, k)))
    return estado


def _resultado(estado):
    (args, kw), = estado["enviados"]
    assert args[2] == "done"
    return kw.get("result") or args[3]


def test_process_job_sin_pedido_no_mide(job_simulado):
    worker.process_job({"id": "j", "track_id": "T"}, {"artist": "a", "title": "t"}, "http://audio")
    r = _resultado(job_simulado)
    assert "parecido" not in r and parecido.huella_valida(r["rasgos"])


def test_process_job_con_semilla_guardada(job_simulado, huellas):
    pedido = {"semilla_track_id": "S", "semilla_rasgos": huellas["semilla"],
              "bases": [{"track_id": t, "rasgos": hh} for t, hh in huellas["bases"]] + [{"track_id": "vieja", "rasgos": None}]}
    worker.process_job({"id": "j", "track_id": "T"}, {"artist": "a", "title": "t"}, "http://audio", None, None, pedido)
    p = _resultado(job_simulado)["parecido"]
    assert p["semilla_track_id"] == "S" and p["puntaje"] == pytest.approx(100.0, abs=1.0)
    assert [t["track_id"] for t in p["linea_base"]["temas"]] == ["b1", "b2", "b3"]  # la base sin huella no cuenta
    assert "semilla_rasgos" not in p
    assert job_simulado["bajadas"] == ["http://audio"]  # no bajó la semilla


def test_process_job_semilla_sin_huella_la_baja_y_la_devuelve(job_simulado, huellas, monkeypatch):
    monkeypatch.setattr(parecido, "huella_de_archivo", lambda path, bpm, ancla, key, sr, dur: huellas["semilla"])
    pedido = {"semilla_track_id": "S", "semilla_rasgos": {"v": 0}, "semilla_audio_url": "http://semilla",
              "semilla": {"bpm": 124, "key": "8A", "first_beat_ms": 250}, "bases": []}
    worker.process_job({"id": "j", "track_id": "T"}, {"artist": "a", "title": "t"}, "http://audio", None, None, pedido)
    p = _resultado(job_simulado)["parecido"]
    assert job_simulado["bajadas"] == ["http://audio", "http://semilla"]
    assert p["semilla_rasgos"] == huellas["semilla"]
    assert p["bruto"] == pytest.approx(100.0, abs=0.5) and p["puntaje"] is None  # sin base, solo bruto


def test_process_job_si_el_parecido_falla_el_analisis_sigue(job_simulado):
    pedido = {"semilla_track_id": "S", "semilla_rasgos": None}  # ni huella ni audio
    worker.process_job({"id": "j", "track_id": "T"}, {"artist": "a", "title": "t"}, "http://audio", None, None, pedido)
    r = _resultado(job_simulado)
    assert "parecido" not in r and r["energy"] == 7


def test_next_job_avisa_la_capacidad_y_pasa_el_pedido(monkeypatch):
    visto = {}

    class R:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"job": {"id": "j"}, "track": {}, "audio_url": "u", "parecido": {"semilla_track_id": "S"}}

    def post(url, headers=None, timeout=None):
        visto["headers"] = headers
        return R()

    monkeypatch.setattr(worker.requests, "post", post)
    _, _, _, extra = worker.next_job()
    assert visto["headers"]["x-worker-capacidades"] == "parecido-1"
    assert extra["parecido"] == {"semilla_track_id": "S"}


# ── cola del banco de calibración ────────────────────────────────────────────

@pytest.fixture
def calibracion(monkeypatch, tmp_path, huellas):
    monkeypatch.setattr(worker, "CALIBRACION_DISPONIBLE", True)
    estado = {"respuestas": [], "llamadas": []}

    def api(action, payload=None):
        estado["llamadas"].append((action, payload))
        return estado["respuestas"].pop(0) if action == "next" else {}

    def download_audio(url):
        p = tmp_path / "cal.wav"
        p.write_bytes(b"x")
        return str(p)

    monkeypatch.setattr(worker, "_parecido_api", api)
    monkeypatch.setattr(worker, "download_audio", download_audio)
    monkeypatch.setattr(worker, "huella_completa", lambda p: huellas["cerca"])
    return estado


def test_calibracion_sin_funcion_se_apaga(calibracion):
    calibracion["respuestas"] = [None]
    assert worker.poll_parecido_calibracion() is False
    assert worker.CALIBRACION_DISPONIBLE is False
    assert worker.poll_parecido_calibracion() is False
    assert len(calibracion["llamadas"]) == 1  # no vuelve a preguntar


def test_calibracion_sin_trabajo(calibracion):
    calibracion["respuestas"] = [{"calibracion": None}]
    assert worker.poll_parecido_calibracion() is False


def test_calibracion_mide_y_guarda(calibracion, huellas):
    calibracion["respuestas"] = [{"calibracion": {"id": "C1", "audio_url": "http://cal"},
                                  "parecido": {"semilla_track_id": "S", "semilla_rasgos": huellas["semilla"],
                                               "bases": [{"track_id": t, "rasgos": hh} for t, hh in huellas["bases"]]}}]
    assert worker.poll_parecido_calibracion() is True
    accion, payload = calibracion["llamadas"][-1]
    assert accion == "resultado" and payload["calibracion_id"] == "C1" and payload["error"] is None
    assert payload["parecido"]["puntaje"] >= 75
    assert payload["parecido"]["toma_rasgos"] == huellas["cerca"]


def test_calibracion_toma_muda_es_determinista(calibracion, monkeypatch):
    def muda(p):
        raise worker.AudioMudo("toma sin audio útil (silencio)")
    monkeypatch.setattr(worker, "huella_completa", muda)
    calibracion["respuestas"] = [{"calibracion": {"id": "C2", "audio_url": "http://cal"}, "parecido": {}}]
    assert worker.poll_parecido_calibracion() is True
    _, payload = calibracion["llamadas"][-1]
    assert payload["parecido"] is None and payload["error"].startswith("determinista:")


def test_huella_completa_de_una_toma_real(tmp_path):
    import soundfile as sf
    p = tmp_path / "toma.wav"
    sf.write(p, club(124, dur=40), SR)
    hh = worker.huella_completa(str(p))
    assert parecido.huella_valida(hh) and abs(hh["bpm"] - 124) < 1.5


def test_coseno_centrado_distingue_formas_opuestas():
    """Los tercios son todos negativos (dB contra el más fuerte): sin centrar, dos formas
    opuestas darían un coseno alto. Centradas en su media son opuestas: 0."""
    graves, agudos = [-1.0, -5.0, -9.0], [-9.0, -5.0, -1.0]
    assert parecido._coseno_centrado(graves, agudos) == pytest.approx(0.0)
    assert parecido._coseno_centrado(graves, graves) == pytest.approx(1.0)
    assert parecido._coseno_centrado([1.0, 1.0], [2.0, 3.0]) is None  # sin forma
