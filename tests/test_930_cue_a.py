"""#930 · dónde va el cue A.

7.6.23: A en el segundo 0 del archivo (en 1.038 de 1.128 temas, saltar a A caía fuera de la
rejilla). 7.6.24: A en el primer DOWNBEAT; en el banco de Germán contra MIK (15-oct) eso dejaba
sonido más de un beat antes de A en la mayoría de los temas (arrancan en un beat que nuestra
rejilla no marca como 1) y el mezclador lo corregía al cargar. 7.6.25: A en un BEAT de la
rejilla, el último que no pasa el primer sonido (#278, `posicionCueASec` del mezclador).
"""
import numpy as np
import pytest

import worker

SR = worker.SR
BPM = 126.0
BEAT_MS = 60000.0 / BPM   # 476,19 ms
BAR_MS = 4 * BEAT_MS      # 1904,76 ms


def en_rejilla(pos_ms, ancla_ms, paso_ms, tol_ms=20):
    fase = (pos_ms - ancla_ms) % paso_ms
    return min(fase, paso_ms - fase) <= tol_ms


def en_downbeat(pos_ms, ancla_ms, bpm=BPM, tol_ms=20):
    return en_rejilla(pos_ms, ancla_ms, 240000.0 / bpm, tol_ms)


@pytest.mark.parametrize("ancla, esperado", [
    (729, int(round(729 - BEAT_MS))),             # la mediana del catálogo: el beat de antes cabe en 253 ms
    (0, 0),
    (None, 0),
    (BAR_MS * 4, 0),                              # justo en un beat: no cae al anterior
    (BAR_MS * 4 + 300, 300),
    (BEAT_MS - 1, int(round(BEAT_MS - 1))),       # un ancla justo antes del 2.º beat
])
def test_sin_dato_de_sonido_a_va_al_primer_beat(ancla, esperado):
    a = worker.posicion_cue_a_ms(ancla, BPM)
    assert a == esperado
    assert 0 <= a < BEAT_MS
    if ancla:
        assert en_rejilla(a, ancla, BEAT_MS)


def test_el_tema_arranca_en_el_beat_2_de_nuestra_rejilla():
    # El caso del banco: suena desde 50 ms y nuestro downbeat queda un beat después (526 ms).
    # 7.6.24 ponía A en 526 (el mezclador lo corregía); MIK y Rekordbox lo ponen en 50.
    ancla = 50 + BEAT_MS + 8 * BAR_MS
    assert worker.posicion_cue_a_ms(ancla, BPM, inicio_ms=50) == 50


@pytest.mark.parametrize("inicio, esperado", [
    (2000, int(round(50 + 4 * BEAT_MS))),         # 2 s de silencio: el beat de 1955 ms, no uno en el silencio
    (50 + 4 * BEAT_MS, int(round(50 + 4 * BEAT_MS))),  # el sonido justo en un beat
    (10, 50),                                     # suena antes del primer beat: el primer beat
    (None, 50),
])
def test_a_nunca_queda_despues_del_primer_sonido(inicio, esperado):
    ancla = 50 + 3 * BAR_MS
    a = worker.posicion_cue_a_ms(ancla, BPM, inicio_ms=inicio)
    assert a == esperado
    assert en_rejilla(a, ancla, BEAT_MS, tol_ms=1)
    if inicio is not None and inicio > 50:
        assert a <= inicio + 1 < a + BEAT_MS + 1


def test_sin_bpm_o_fuera_de_rango_queda_en_0():
    assert worker.posicion_cue_a_ms(729, None, 0) == 0
    assert worker.posicion_cue_a_ms(729, 0) == 0
    assert worker.posicion_cue_a_ms(729, 300) == 0
    assert worker.posicion_cue_a_ms("x", BPM) == 0
    assert worker.posicion_cue_a_ms(-50, BPM) == 0


def test_inicio_audible_igual_que_el_mezclador():
    # inicioAudibleSec: primer pico que pasa el 2 % del máximo, en la escala de la duración.
    picos = [0.0] * 100 + [0.01] * 50 + [0.5] * 850
    assert worker.inicio_audible_ms(picos, 100000) == pytest.approx(15000)   # 0,01 no pasa el 2 % de 0,5
    picos[120] = 0.011
    assert worker.inicio_audible_ms(picos, 100000) == pytest.approx(12000)
    assert worker.inicio_audible_ms([0.0] * 50, 1000) is None
    assert worker.inicio_audible_ms([1.0] * 5, 1000) is None
    assert worker.inicio_audible_ms(None, 1000) is None
    assert worker.inicio_audible_ms([1.0] * 50, 0) is None
    y = np.zeros(SR)
    y[SR // 4:] = 0.5
    assert worker.inicio_audible_ms(y, 1000) == pytest.approx(250, abs=0.1)   # sobre muestras: exacto


def test_cuantizar_pone_a_en_el_beat_del_primer_sonido_y_no_toca_la_fase_del_resto():
    ancla = 4 * BAR_MS + 729
    cues = [
        {"number": 0, "label": "MIX-IN", "positionMs": 0},     # como lo dejaba 7.6.23
        {"number": 1, "label": "BASS-IN", "positionMs": int(ancla + 16 * BAR_MS + 3)},
        {"number": 7, "label": "MIX-OUT", "positionMs": int(ancla + 96 * BAR_MS - 5)},
    ]
    out = worker.cuantizar_cues(cues, BPM, ancla, 400000, inicio_ms=0)
    a = [c for c in out if c["number"] == 0][0]["positionMs"]
    assert a == int(round(729 - BEAT_MS))
    for c in out:
        tol = 2 if c["number"] else 1
        paso = BAR_MS if c["number"] else BEAT_MS
        assert en_rejilla(c["positionMs"], ancla, paso, tol_ms=tol), c


def test_cuantizar_mueve_un_a_viejo_que_quedo_en_el_ancla():
    # El bug de antes de 7.6.x: A cuantizado al downbeat más cercano (0 -> 907 ms) quedaba en el ancla.
    ancla = 2 * BAR_MS + 907
    out = worker.cuantizar_cues([{"number": 0, "label": "MIX-IN", "positionMs": int(ancla)}], BPM, ancla, 300000,
                                inicio_ms=900)
    assert out[0]["positionMs"] == int(round(907 - BEAT_MS))


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


def test_detect_cues_pone_a_donde_empieza_a_sonar_en_fase_con_los_demas():
    y, ancla = _tema(intro_silencio_ms=300)
    cues = worker.detect_cues(y, SR, BPM, ancla)
    assert cues and cues[0]["number"] == 0
    assert cues[0]["positionMs"] == 300 == worker.posicion_cue_a_ms(ancla, BPM, 300)
    for c in cues:
        assert en_downbeat(c["positionMs"], ancla, tol_ms=2), c


def test_cues_respaldo_tambien():
    y, ancla = _tema(intro_silencio_ms=650, compases=150)
    cues = worker.cues_respaldo(y, SR, BPM, ancla)
    assert cues[0]["number"] == 0 and abs(cues[0]["positionMs"] - 650) <= 1
    for c in cues:
        assert en_downbeat(c["positionMs"], ancla, tol_ms=2), c


def test_sin_bpm_el_plan_c_deja_a_en_0():
    assert worker.cues_por_tiempo(200000)[0]["positionMs"] == 0


def test_detect_cues_tema_que_arranca_en_el_beat_2():
    # Suena desde 0, pero el primer downbeat (ancla) cae un beat más tarde: A en 0, no en el ancla.
    y, ancla = _tema(intro_silencio_ms=0)
    y = np.concatenate([0.6 * np.sin(2 * np.pi * 55 * np.arange(int(BEAT_MS / 1000 * SR)) / SR), y]).astype(np.float32)
    cues = worker.detect_cues(y, SR, BPM, ancla + BEAT_MS)
    assert cues and cues[0]["number"] == 0 and cues[0]["positionMs"] == 0
    for c in cues[1:]:
        assert en_downbeat(c["positionMs"], ancla + BEAT_MS, tol_ms=2), c
