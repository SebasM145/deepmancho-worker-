"""7.6.25 · Hot cues y Salida de la metodología propia (dj-connect, auditoría del 15-nov, §6.2).

C1: hasta 8 cues; Inicio y Salida siempre; del medio, solo los claros: se oyen y le ganan al azar.
C3: todos en el «1» y en frontera de 8 compases contados desde el primer «1».
C5: en temas de 5 min o más, la Salida deja de 32 a 48 compases de cola; en temas cortos va en la
    última bajada con 16 compases de cola, y si no la hay se dice. Sin la alarma `mixout_temprano`.
La medida con música real está en el CHANGELOG; aquí van las reglas, con audio sintético.
"""
import numpy as np
import pytest

import worker

SR = worker.SR
BPM = 126.0
BAR_S = 240.0 / BPM
BAR_MS = BAR_S * 1000


def tema(partes, silencio_ms=0, semilla=7):
    """partes = [(compases, nivel)]: 0 = bombo solo, 1 = + bajo, 2 = + bajo y hats. Cada compás
    lleva un poco de ruido propio: sin eso, dos compases iguales no tendrían ni azar que medir."""
    rng = np.random.default_rng(semilla)
    trozos = [np.zeros(int(silencio_ms / 1000 * SR))]
    for n, nivel in partes:
        for _ in range(n):
            t = np.arange(int(round(BAR_S * SR))) / SR
            y = 0.8 * ((t % (60.0 / BPM)) < 0.03) * np.sin(2 * np.pi * 55 * t)
            if nivel >= 1:
                y = y + 0.3 * np.sin(2 * np.pi * 110 * t)
            if nivel >= 2:
                y = y + 0.25 * rng.standard_normal(t.size) * ((t % (30.0 / BPM)) < 0.02)
            y = y + 0.01 * rng.uniform(0.9, 1.1) * rng.standard_normal(t.size)
            trozos.append(y)
    return np.concatenate(trozos).astype(np.float32)


def compases(cues, origen_ms=0.0):
    return [(c["positionMs"] - origen_ms) / BAR_MS for c in cues]


# 184 compases (5,8 min): cambios en 16, 48, 64, 96, 112 y 144.
LARGO = [(16, 1), (32, 2), (16, 0), (32, 2), (16, 1), (32, 2), (40, 0)]


def test_c3_todos_los_cues_en_frontera_de_8_desde_el_primer_uno():
    # El ancla (primer bombo detectado) cae 3 compases después del primer «1»: las frases se
    # cuentan desde el primer «1», no desde el ancla.
    y = tema(LARGO, silencio_ms=300)
    cues = worker.detect_cues(y, SR, BPM, 300 + 3 * BAR_MS)
    assert cues[0]["number"] == 0 and cues[0]["positionMs"] == 300
    for b in compases(cues, 300):
        assert abs(b - round(b)) < 0.02 and round(b) % 8 == 0, f"fuera de la frase de 8: {b}"


def test_c1_inicio_y_salida_siempre_y_la_salida_es_el_cue_7():
    cues = worker.detect_cues(tema(LARGO), SR, BPM, 0)
    assert cues[0]["number"] == 0 and cues[0]["label"] == "MIX-IN"
    assert cues[-1]["number"] == 7 and cues[-1]["label"] == "MIX-OUT"
    numeros = [c["number"] for c in cues]
    assert numeros == sorted(numeros) and len(set(numeros)) == len(numeros) and len(cues) <= 8


def test_c1_los_cambios_de_seccion_quedan_y_van_en_orden():
    cues = worker.detect_cues(tema(LARGO), SR, BPM, 0)
    medio = [round(b) for b in compases(cues)[1:-1]]
    for cambio in (16, 48, 64, 96, 112):
        assert cambio in medio, f"falta el cambio del compás {cambio}: {medio}"
    assert all(c["confidence"] >= worker.CONF_CUE_MIN for c in cues), "un cue que pasó la vara no se dibuja dudoso"


def test_c1_ningun_cue_del_medio_cambia_menos_que_el_azar():
    # Los únicos cambios de este tema están a MEDIA frase (compases 20, 52, 84…): eso es lo que
    # mide el azar. En las fronteras de 8 no cambia nada, así que no hay cue del medio que poner.
    partes = [(20, 1), (32, 2), (32, 0), (32, 2), (32, 1), (36, 2)]
    info = {}
    cues = worker.detect_cues(tema(partes), SR, BPM, 0, info)
    assert [c["number"] for c in cues] == [0, 7], [(c["number"], round(c["positionMs"] / BAR_MS)) for c in cues]


def test_c1_menos_de_8_cuando_el_tema_no_da():
    # Un solo cambio de sección: quedan Inicio, ese cambio y Salida (antes se rellenaba hasta 8).
    partes = [(64, 1), (120, 2)]
    cues = worker.detect_cues(tema(partes), SR, BPM, 0)
    medio = [round(b) for b in compases(cues)[1:-1]]
    assert 64 in medio and len(cues) < 8, medio


def test_c5_tema_largo_la_salida_deja_de_32_a_48_compases_y_cae_en_la_bajada():
    info = {}
    cues = worker.detect_cues(tema(LARGO), SR, BPM, 0, info)
    h = round(compases(cues)[-1])
    assert h == 144 and info["salida"] == "bajada"          # la bajada está a 40 compases del final
    assert worker.SALIDA_COLA_MIN <= 184 - h <= worker.SALIDA_COLA_MAX
    assert cues[-1]["confidence"] == 1.0


def test_c5_tema_largo_sin_bajada_igual_deja_cola_y_no_es_alarma():
    # 184 compases iguales: no hay bajada, pero la cola para mezclar sí.
    info = {}
    cues = worker.detect_cues(tema([(184, 2)]), SR, BPM, 0, info)
    h = round(compases(cues)[-1])
    assert worker.SALIDA_COLA_MIN <= 184 - h <= worker.SALIDA_COLA_MAX
    assert info["salida"] == "sin_bajada_largo" and cues[-1]["confidence"] == 0.6
    assert [c["number"] for c in cues] == [0, 7], "un tema plano no tiene cues del medio"
    assert worker.sanity_check({"bpm": 126, "cue_points": cues, "_salida": info["salida"]}, 184 * BAR_MS) == ([], 1.0)


def test_c5_un_break_no_es_la_salida():
    # Baja 8 compases a 40 del final y la música VUELVE; la bajada de verdad está a 32.
    partes = [(16, 1), (128, 2), (8, 0), (8, 2), (32, 0)]      # 192 compases
    info = {}
    cues = worker.detect_cues(tema(partes), SR, BPM, 0, info)
    assert round(compases(cues)[-1]) == 160 and info["salida"] == "bajada"


def test_c5_tema_corto_ultima_bajada_con_16_compases_de_cola():
    # 80 compases (2,5 min): baja en el 32 (vuelve) y en el 56 (ya no vuelve), a 24 del final.
    partes = [(32, 2), (8, 0), (16, 2), (24, 0)]
    info = {}
    cues = worker.detect_cues(tema(partes), SR, BPM, 0, info)
    assert round(compases(cues)[-1]) == 56 and info["salida"] == "bajada"


def test_c5_tema_corto_sin_bajada_se_dice():
    info = {}
    cues = worker.detect_cues(tema([(80, 2)]), SR, BPM, 0, info)
    h = round(compases(cues)[-1])
    assert info["salida"] == "sin_bajada" and 80 - h >= worker.SALIDA_COLA_CORTA and h % 8 == 0
    assert cues[-1]["confidence"] == 0.5                      # mezcla corta, no punteado
    problemas, conf = worker.sanity_check({"bpm": 126, "cue_points": cues, "_salida": info["salida"]}, 80 * BAR_MS)
    assert problemas == ["salida_sin_bajada"] and conf == 0.8


def test_c5_tema_muy_corto_sin_cola_se_dice():
    info = {}
    cues = worker.detect_cues(tema([(26, 2)]), SR, BPM, 0, info)
    assert info["salida"] == "sin_cola" and cues[-1]["number"] == 7 and cues[-1]["confidence"] < 0.45
    problemas, _ = worker.sanity_check({"bpm": 126, "cue_points": cues, "_salida": "sin_cola"}, 26 * BAR_MS)
    assert "salida_sin_cola" in problemas


def test_c5_ya_no_existe_la_alarma_mixout_temprano():
    # H al 75 % de un tema de 6 min, con 90 s de cola: antes saltaba `mixout_temprano:0.75`.
    cues = [{"label": "MIX-IN", "positionMs": 0}, {"label": "MIX-OUT", "positionMs": 270000}]
    assert worker.sanity_check({"bpm": 124, "cue_points": cues}, 360000) == ([], 1.0)


@pytest.mark.parametrize("fin1,largo,esperado", [
    (184, True, (144, "bajada")),
    (80, False, (56, "bajada")),
])
def test_elegir_salida_puro(fin1, largo, esperado):
    nivel = np.full(fin1, -20.0)
    nivel[esperado[0]:] = -26.0
    assert worker.elegir_salida(nivel, fin1, largo) == esperado


def test_elegir_salida_corto_no_deja_medio_tema_de_cola():
    # La única bajada está en el compás 16 de 80: una cola de 64 compases no es una salida.
    nivel = np.full(80, -20.0)
    nivel[16:24] = -30.0
    h, estado = worker.elegir_salida(nivel, 80, False)
    assert h >= 40 and estado == "sin_bajada"


def test_cuantizar_lleva_al_compas_y_a_la_frase_de_8():
    ancla = 2 * BAR_MS + 300          # primer «1» en 300 ms
    cues = [
        {"number": 0, "label": "MIX-IN", "positionMs": 0},
        {"number": 1, "label": "BASS-IN", "positionMs": int(300 + 16 * BAR_MS + BAR_MS / 4 + 20)},   # un beat tarde
        {"number": 7, "label": "MIX-OUT", "positionMs": int(300 + 97 * BAR_MS)},                      # un compás tarde
    ]
    out = worker.cuantizar_cues(cues, BPM, ancla, 600000)
    assert [round((c["positionMs"] - 300) / BAR_MS, 2) for c in out] == [0, 16, 96]


def test_cuantizar_plan_b_solo_al_compas():
    cues = [{"number": 0, "label": "MIX-IN", "positionMs": 0, "origen": "grilla"},
            {"number": 1, "label": "BASS-IN", "positionMs": int(4 * BAR_MS + BAR_MS / 4), "origen": "grilla"}]
    out = worker.cuantizar_cues(cues, BPM, 0, 60000)
    assert round(out[1]["positionMs"] / BAR_MS, 2) == 4


def test_plan_b_cuenta_las_frases_desde_el_primer_uno():
    # 16 compases (plan B) y el ancla 2 compases después del primer «1».
    y = tema([(8, 2), (8, 0)], silencio_ms=200)
    assert worker.detect_cues(y, SR, BPM, 200 + 2 * BAR_MS) is None
    cues = worker.cues_respaldo(y, SR, BPM, 200 + 2 * BAR_MS)
    assert cues[0]["positionMs"] == 200
    for b in compases(cues, 200)[1:]:
        assert abs(b - round(b)) < 0.02 and round(b) % 4 == 0, b
