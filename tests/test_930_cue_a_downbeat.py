"""#930 · el cue A va al PRIMER DOWNBEAT de la rejilla, no al segundo 0 del archivo.

En producción (11-oct), A estaba en 0 ms en los 1.128 temas `structure_v1`, pero el ancla
(first_beat_offset_ms) solo estaba en ≤ 30 ms en 90: en 1.038 temas saltar a A caía fuera del
compás. Regla: A = ancla − k compases, el más temprano ≥ 0. Sin rejilla (plan C) sigue en 0.
"""
import numpy as np
import pytest

import worker

SR = worker.SR
BPM = 126.0
BAR_MS = 240000.0 / BPM  # 1904,76 ms


def en_downbeat(pos_ms, ancla_ms, bpm=BPM, tol_ms=20):
    bar = 240000.0 / bpm
    fase = (pos_ms - ancla_ms) % bar
    return min(fase, bar - fase) <= tol_ms


@pytest.mark.parametrize("ancla, esperado", [
    (729, 729),                                   # la mediana del catálogo: ya es el primer compás
    (0, 0),
    (None, 0),
    (BAR_MS * 4, 0),                              # justo en un compás: no cae al anterior
    (BAR_MS * 4 + 300, 300),                      # bombo que entra en el compás 4 con 300 ms de silencio
    (31200, int(round(31200 - 16 * BAR_MS))),     # intro de 31 s sin ritmo: el downbeat de la rejilla al inicio
    (BAR_MS - 1, int(round(BAR_MS - 1))),         # un ancla justo antes del 2.º compás
])
def test_primer_downbeat(ancla, esperado):
    a = worker.primer_downbeat_ms(ancla, BPM)
    assert a == esperado
    assert 0 <= a < BAR_MS
    if ancla:
        assert en_downbeat(a, ancla)


def test_sin_bpm_o_fuera_de_rango_queda_en_0():
    assert worker.primer_downbeat_ms(729, None) == 0
    assert worker.primer_downbeat_ms(729, 0) == 0
    assert worker.primer_downbeat_ms(729, 300) == 0
    assert worker.primer_downbeat_ms("x", BPM) == 0
    assert worker.primer_downbeat_ms(-50, BPM) == 0


def test_cuantizar_pone_a_en_el_primer_downbeat_y_no_toca_la_fase_del_resto():
    ancla = 4 * BAR_MS + 729
    cues = [
        {"number": 0, "label": "MIX-IN", "positionMs": 0},     # como lo dejaba 7.6.23
        {"number": 1, "label": "BASS-IN", "positionMs": int(ancla + 16 * BAR_MS + 3)},
        {"number": 7, "label": "MIX-OUT", "positionMs": int(ancla + 96 * BAR_MS - 5)},
    ]
    out = worker.cuantizar_cues(cues, BPM, ancla, 400000)
    a = [c for c in out if c["number"] == 0][0]["positionMs"]
    assert a == 729
    for c in out:
        assert en_downbeat(c["positionMs"], ancla, tol_ms=2), c


def test_cuantizar_mueve_un_a_viejo_que_quedo_en_el_ancla():
    # El bug de antes de 7.6.x: A cuantizado al downbeat más cercano (0 -> 907 ms) quedaba en el ancla.
    ancla = 2 * BAR_MS + 907
    out = worker.cuantizar_cues([{"number": 0, "label": "MIX-IN", "positionMs": int(ancla)}], BPM, ancla, 300000)
    assert out[0]["positionMs"] == 907


def _tema(intro_silencio_ms, compases=160):
    """Silencio, 4 compases de pad sin bombo y luego bombo + bajo con cambios de frase."""
    rng = np.random.default_rng(3)
    trozos = [np.zeros(int(intro_silencio_ms / 1000 * SR))]
    t = np.arange(int(4 * BAR_MS / 1000 * SR)) / SR
    trozos.append(0.05 * np.sin(2 * np.pi * 220 * t))
    for n, nivel in [(12, 1), (32, 2), (16, 0), (32, 2), (16, 1), (32, 2), (compases - 140, 0)]:
        t = np.arange(int(n * BAR_MS / 1000 * SR)) / SR
        y = 0.8 * ((t % (60.0 / BPM)) < 0.03) * np.sin(2 * np.pi * 55 * t)
        if nivel >= 1:
            y = y + 0.3 * np.sin(2 * np.pi * 110 * t)
        if nivel >= 2:
            y = y + 0.25 * rng.standard_normal(t.size) * ((t % (30.0 / BPM)) < 0.02)
        trozos.append(y)
    return np.concatenate(trozos).astype(np.float32), intro_silencio_ms + 4 * BAR_MS


def test_detect_cues_pone_a_en_el_primer_downbeat_en_fase_con_los_demas():
    y, ancla = _tema(intro_silencio_ms=300)
    cues = worker.detect_cues(y, SR, BPM, ancla)
    assert cues and cues[0]["number"] == 0
    assert cues[0]["positionMs"] == 300 == worker.primer_downbeat_ms(ancla, BPM)
    for c in cues:
        assert en_downbeat(c["positionMs"], ancla, tol_ms=2), c


def test_cues_respaldo_tambien():
    y, ancla = _tema(intro_silencio_ms=650, compases=150)
    cues = worker.cues_respaldo(y, SR, BPM, ancla)
    assert cues[0]["number"] == 0 and cues[0]["positionMs"] == 650
    for c in cues:
        assert en_downbeat(c["positionMs"], ancla, tol_ms=2), c


def test_sin_bpm_el_plan_c_deja_a_en_0():
    assert worker.cues_por_tiempo(200000)[0]["positionMs"] == 0
