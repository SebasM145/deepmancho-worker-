"""#573: el examen CM2 usa un golden set SINTÉTICO (BPM y fase de bombo conocidos), sin
depender de temas del catálogo ni de la red. Tiene que aprobar con el detector actual y
reprobar si el ancla de un tema se corre."""
import os
import shutil
import sys

import numpy as np
import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import soundfile as sf  # noqa: E402
import worker  # noqa: E402

hay_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="sin ffmpeg")


@pytest.fixture
def sin_red(monkeypatch):
    def prohibido(*a, **k):
        raise AssertionError("el examen no debe usar la red")
    monkeypatch.setattr(worker.requests, "get", prohibido)
    monkeypatch.setattr(worker.requests, "post", prohibido)


@pytest.mark.parametrize("i", range(len(worker.GOLDEN_SINTETICO)))
def test_el_bombo_cae_en_su_fase(tmp_path, i):
    """Sin codificar: el detector encuentra cada bombo a menos de 10 ms de su oro."""
    nombre, bpm, fase, kick_hz, caida, bajo = worker.GOLDEN_SINTETICO[i]
    p = tmp_path / "t.wav"
    sf.write(p, worker.tema_golden(bpm, fase, kick_hz, caida, bajo, semilla=i), 44100)
    r = worker.compute_anchor(str(p), bpm)
    assert abs(worker._wrap(r["ancla_ms"] - fase, 60000.0 / bpm)) <= 10.0, nombre


def test_los_temas_traen_las_trampas():
    """Intro sin bombo y bajo a contratiempo en la banda del bombo: si no, el examen es trivial."""
    nombre, bpm, fase, kick_hz, caida, bajo = worker.GOLDEN_SINTETICO[2]
    x = worker.tema_golden(bpm, fase, kick_hz, caida, bajo)[:, 0]
    beat = int(60.0 / bpm * 44100)
    def graves(seg):  # energía por debajo de 130 Hz (la banda del bombo)
        e = np.abs(np.fft.rfft(seg)) ** 2
        f = np.fft.rfftfreq(len(seg), 1 / 44100)
        return e[f < 130].sum() / len(seg)
    intro = x[: 8 * 4 * beat]
    cuerpo = x[10 * 4 * beat: 18 * 4 * beat]
    assert graves(intro) < 0.01 * graves(cuerpo)       # la intro no tiene bombo ni bajo
    assert bajo >= 0.5                                 # y en el cuerpo el bajo pesa


@hay_ffmpeg
def test_examen_aprueba_sin_red(sin_red, capsys):
    assert worker.golden_exam() is True
    out = capsys.readouterr().out
    assert out.count("✅") == len(worker.GOLDEN_PAIRS) == 3
    assert "RESULTADO: APROBADO" in out


@hay_ffmpeg
def test_examen_reprueba_si_un_ancla_se_corre(sin_red, monkeypatch, capsys):
    original = worker.compute_anchor
    llamadas = []

    def corrido(path, bpm):
        r = original(path, bpm)
        llamadas.append(1)
        if len(llamadas) == 3:              # el tercer tema, 25 ms tarde
            r = {**r, "ancla_ms": r["ancla_ms"] + 25.0}
        return r
    monkeypatch.setattr(worker, "compute_anchor", corrido)
    assert worker.golden_exam() is False
    assert "NO APROBADO" in capsys.readouterr().out


def test_examen_sin_ffmpeg_no_aprueba(sin_red, monkeypatch):
    """Si no se puede codificar el MP3, no hay examen aprobado (CM2 no escribe nada)."""
    monkeypatch.setattr(worker, "make_rendition", lambda *a, **k: None)
    assert worker.golden_exam() is False


def test_par_con_bpm_distintos_compara_cada_tema_con_su_periodo(monkeypatch, capsys):
    """Regresión: el par 128 × 123 BPM daba +132 ms con anclas perfectas (período promedio)."""
    def perfecta(i):
        nombre, bpm, fase, *_ = worker.GOLDEN_SINTETICO[i]
        return {"ancla_ms": fase + 120 * 60000.0 / bpm - 6.0, "bpm_real": bpm, "residuo_ms": 1.0}
    monkeypatch.setattr(worker, "ancla_golden", perfecta)
    assert worker.golden_exam() is True
    assert "error +0.0 ms ✅" in capsys.readouterr().out
