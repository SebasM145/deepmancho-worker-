"""Plan B y C de hot cues: ningun tema queda sin cues."""
import os
import sys

import numpy as np

# El worker exige estas variables al importarse; en pruebas no se usan.
os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import worker  # noqa: E402

SR = 11025
BPM = 126.0
BAR_S = 4 * 60.0 / BPM


def tema(compases_por_parte, anchor_s=0.2):
    """Tema sintetico: bombo en negras + hats y bajo segun la parte (0 = solo bombo)."""
    partes = []
    for n, nivel in compases_por_parte:
        t = np.arange(int(n * BAR_S * SR)) / SR
        beat = (t % (60.0 / BPM)) < 0.03
        y = 0.8 * beat * np.sin(2 * np.pi * 55 * t)
        if nivel >= 1:
            y += 0.3 * np.sin(2 * np.pi * 110 * t)
        if nivel >= 2:
            y += 0.2 * np.random.default_rng(1).standard_normal(t.size) * ((t % (30.0 / BPM)) < 0.02)
        partes.append(y)
    return np.concatenate([np.zeros(int(anchor_s * SR)), *partes]).astype(np.float32), anchor_s * 1000


def test_boceto_corto_tiene_inicio_y_salida():
    # 16 compases (~30 s): el detector estructural exige 24 y devolvia None.
    y, ancla = tema([(8, 2), (8, 0)])
    assert worker.detect_cues(y, SR, BPM, ancla) is None
    cues = worker.cues_respaldo(y, SR, BPM, ancla)
    nums = [c["number"] for c in cues]
    assert nums[0] == 0 and cues[0]["positionMs"] == 0
    assert 7 in nums, "debe haber cue de salida"
    assert all(c["confidence"] < 0.5 for c in cues if c["number"] != 0)
    assert all(c["origen"] == "grilla" for c in cues)


def test_cues_caen_en_la_rejilla_de_frase():
    y, ancla = tema([(16, 0), (32, 2), (16, 1), (32, 2), (16, 0)])
    cues = worker.cues_respaldo(y, SR, BPM, ancla)
    bar_ms = BAR_S * 1000
    for c in cues[1:]:
        compases = (c["positionMs"] - ancla) / bar_ms
        assert abs(compases - round(compases)) < 0.02
        assert round(compases) % 8 == 0, f"cue {c['number']} fuera de frase: {compases}"
    # la salida deja cola para mezclar
    h = [c for c in cues if c["number"] == 7][0]
    assert (len(y) / SR * 1000 - h["positionMs"]) / bar_ms >= 16


def test_audio_minimo_y_sin_bpm():
    y, ancla = tema([(1, 1)])
    assert [c["number"] for c in worker.cues_respaldo(y, SR, BPM, ancla)] == [0]
    assert worker.cues_respaldo(y, SR, None, ancla) is None
    c = worker.cues_por_tiempo(200000)
    assert [x["number"] for x in c] == [0, 7] and c[1]["positionMs"] == 170000
    assert all(x["origen"] == "tiempo" for x in c)


def test_energia_1_10():
    y, ancla = tema([(16, 0), (32, 2), (16, 0)])
    for c in worker.cues_respaldo(y, SR, BPM, ancla):
        assert 1 <= c["energy"] <= 10
