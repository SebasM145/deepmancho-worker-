"""#860 · hot cues y energía calibrados contra Mixed In Key.

La medida real está en bancos/banco_mik.py (50 temas de Germán, 60 % -> 76 %); aquí van las
reglas que la sostienen, con audio sintético para que corran en el CI sin el audio de nadie.
"""
import numpy as np

import worker

SR = worker.SR
BPM = 126.0
BAR_S = 240.0 / BPM


def tema(partes, intro_sin_bombo=0):
    """partes = [(compases, nivel)]: 0 = bombo solo, 1 = + bajo, 2 = + bajo y hats.
    `intro_sin_bombo` compases de pad al inicio: el ancla (primer bombo) queda ahí."""
    rng = np.random.default_rng(7)
    trozos = []
    if intro_sin_bombo:
        t = np.arange(int(intro_sin_bombo * BAR_S * SR)) / SR
        trozos.append(0.05 * np.sin(2 * np.pi * 220 * t))
    for n, nivel in partes:
        t = np.arange(int(n * BAR_S * SR)) / SR
        beat = (t % (60.0 / BPM)) < 0.03
        y = 0.8 * beat * np.sin(2 * np.pi * 55 * t)
        if nivel >= 1:
            y = y + 0.3 * np.sin(2 * np.pi * 110 * t)
        if nivel >= 2:
            y = y + 0.25 * rng.standard_normal(t.size) * ((t % (30.0 / BPM)) < 0.02)
        trozos.append(y)
    return np.concatenate(trozos).astype(np.float32), intro_sin_bombo * BAR_S * 1000


# Cambios en los compases 16, 48, 64, 96, 112 y 144 contados desde el INICIO del archivo.
ESTRUCTURA = [(12, 1), (32, 2), (16, 0), (32, 2), (16, 1), (32, 2), (40, 0)]


def _compases_desde_el_inicio(cues):
    return [c["positionMs"] / 1000 / BAR_S for c in cues if c["number"] != 0]


def test_las_frases_se_cuentan_desde_el_inicio_del_archivo_aunque_el_bombo_entre_tarde():
    # El bombo entra en el compás 4: contando desde el ancla, los cues caían en 12, 44, 60…
    # (a 4 compases de los cambios). MIK y la música los tienen en 16, 48, 64…
    y, ancla = tema(ESTRUCTURA, intro_sin_bombo=4)
    cues = worker.detect_cues(y, SR, BPM, ancla)
    assert cues and cues[0]["positionMs"] == 0
    compases = _compases_desde_el_inicio(cues)
    for b in compases:
        assert abs(b - round(b)) < 0.02 and round(b) % 4 == 0, f"fuera de la rejilla de frase: {compases}"
    for cambio in (16, 48, 64, 96):
        assert any(abs(b - cambio) < 0.1 for b in compases), f"falta el cambio del compás {cambio}: {compases}"


def test_la_salida_queda_en_el_ultimo_cuarto_y_deja_cola():
    y, ancla = tema(ESTRUCTURA, intro_sin_bombo=4)
    cues = worker.detect_cues(y, SR, BPM, ancla)
    h = cues[-1]
    assert h["number"] == len(cues) - 1 and h["label"] in ("MIX-OUT", "VOCALS", "DROP 2")
    total = len(y) / SR / BAR_S
    salida = h["positionMs"] / 1000 / BAR_S
    assert salida >= 0.75 * (total - 1) - 4, f"salida muy temprano: compás {salida:.0f} de {total:.0f}"
    assert total - salida >= worker.CUE_COLA - 1, "la salida tiene que dejar cola para mezclar"


def test_dos_cues_nunca_a_menos_de_una_frase():
    y, ancla = tema(ESTRUCTURA, intro_sin_bombo=4)
    compases = sorted(_compases_desde_el_inicio(worker.detect_cues(y, SR, BPM, ancla)))
    assert all(b - a >= worker.CUE_SEPARACION - 0.1 for a, b in zip(compases, compases[1:])), compases


def test_energia_del_tema_en_la_escala_de_mik():
    # Lo más fuerte posible (e01 = 1) da 8, no 10; el silencio sigue en 1. En el banco, el tech
    # house pasó de 8 (MIK 6) a 6.
    bandas_llenas = {"high": [1.0] * 10}
    assert worker.compute_energy(np.array([1.0]), bandas_llenas) == 8
    assert worker.compute_energy(np.array([0.0]), {"high": [0.0]}) == 1
    tipico = 0.76   # e01 medio de los 50 temas del banco
    rms = np.array([((tipico - 0.4 * 0.40) / 0.6 / 3.0) ** 2])
    assert worker.compute_energy(rms, {"high": [0.40]}) == 6
