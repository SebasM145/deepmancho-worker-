"""W4 (30-sep-2026): el verificador no baja audio que no puede medir y marca los
errores deterministas para que la plataforma no los reintente."""
import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import grid_verifier as gv  # noqa: E402

TEMA = {"bpm": 124, "bpm_fine": 0, "first_beat_detected_ms": 250, "grid_source": "detectada",
        "duration_seconds": 64}


def medida_buena():
    return {"anchor_ms": 250, "bpm_fine": 0.01, "conf": 0.9, "peak_ratio": 3.2,
            "windows_good": 7, "windows_total": 8, "residual_beats": 0.02}


@pytest.mark.parametrize("cambio,motivo", [
    ({}, None),
    ({"bpm": None}, "no_bpm"),
    ({"first_beat_detected_ms": None}, "no_anchor"),
    ({"duration_seconds": 12}, "too_short"),
    ({"duration_seconds": None}, None),  # sin duración conocida: se mide y decide measure_track
])
def test_motivo_sin_medir(cambio, motivo):
    assert gv.motivo_sin_medir({**TEMA, **cambio}) == motivo


@pytest.mark.parametrize("campo,valor", [
    ("anchor_ms", None), ("conf", math.nan), ("bpm_fine", 2.0), ("residual_beats", math.inf),
    ("windows_total", "x"),
])
def test_medida_invalida(campo, valor):
    assert gv.medida_invalida(medida_buena()) is None
    assert gv.medida_invalida({**medida_buena(), campo: valor}) == campo


def test_codigo_error():
    assert gv.codigo_error(RuntimeError("too_short")) == "determinista:too_short"
    assert gv.codigo_error(RuntimeError("bad_measurement")) == "determinista:bad_measurement"
    # Fallas de red o de ffmpeg pueden salir bien al reintentar: sin prefijo.
    assert gv.codigo_error(RuntimeError("ffmpeg_failed:x")) == "ffmpeg_failed:x"


class Plataforma:
    """grid-verify-next / grid-verify-result simulados; guarda lo reportado."""

    def __init__(self, track):
        self.track = track
        self.reportes = []

    def api(self, path, body, timeout=30):
        if path == "grid-verify-next":
            return {"job": {"id": "j1", "track_id": "t1"}, "track": {**self.track, "path": "x.mp3"},
                    "audio_url": "http://audio.invalid/x.mp3"}
        self.reportes.append(body)
        return {"ok": True, "result": {}}


def test_sin_ancla_no_descarga_y_reporta_determinista(monkeypatch):
    plat = Plataforma({**TEMA, "first_beat_detected_ms": None})
    monkeypatch.setattr(gv, "api", plat.api)
    monkeypatch.setattr(gv, "download", lambda *a: pytest.fail("no debía descargar"))
    assert gv.process_one() is True
    assert plat.reportes == [{"job_id": "j1", "track_id": "t1", "ok": False,
                              "error": "esperando_analisis:no_anchor"}]


def tema_sintetico(bpm=124.0, ancla_s=0.25, dur_s=64, sr=44100):
    """Bombo de 55 Hz en negras desde el ancla (como synth.py de la auditoría)."""
    t = np.arange(int(dur_s * sr)) / sr
    fase = (t - ancla_s) % (60.0 / bpm)
    golpe = (t >= ancla_s) & (fase < 0.08)
    return (0.8 * golpe * np.exp(-fase * 40) * np.sin(2 * np.pi * 55 * t)).astype(np.float32), sr


def test_medida_real_pasa_la_validacion(monkeypatch, tmp_path):
    pcm, sr = tema_sintetico()
    plat = Plataforma(TEMA)
    monkeypatch.setattr(gv, "api", plat.api)
    monkeypatch.setattr(gv, "download", lambda url, dest: 1)
    monkeypatch.setattr(gv, "decode_file", lambda f: (pcm, sr))
    gv.process_one()
    rep = plat.reportes[0]
    assert rep["ok"] is True, rep
    assert gv.medida_invalida(rep["measurement"]) is None
    assert abs(rep["measurement"]["anchor_ms"] - 250) < 10


def test_medida_fuera_de_rango_no_se_envia(monkeypatch):
    pcm, sr = tema_sintetico()
    plat = Plataforma(TEMA)
    monkeypatch.setattr(gv, "api", plat.api)
    monkeypatch.setattr(gv, "download", lambda url, dest: 1)
    monkeypatch.setattr(gv, "decode_file", lambda f: (pcm, sr))
    monkeypatch.setattr(gv, "measurement_payload", lambda m, p: {**medida_buena(), "conf": math.nan})
    gv.process_one()
    assert plat.reportes == [{"job_id": "j1", "track_id": "t1", "ok": False,
                              "error": "determinista:bad_measurement"}]
