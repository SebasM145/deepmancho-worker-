"""#1064 · el cue B (BASS-IN) automático va al downbeat donde ENTRA EL BAJO.

Decisión de Germán (18-oct): el mezclador hace en B el intercambio de graves; en 9 de 21
entrantes de «Piso 2» el bajo ya sonaba varios compases antes de B (dos bajos a la vez).
Audio sintético para que corra en el CI sin el audio de nadie.
"""
import numpy as np

import worker

SR = worker.SR
BPM = 126.0
BAR_S = 240.0 / BPM


def tema(partes, bombo_largo=False, seed=3):
    """partes = [(compases, capas)]; capas: "k" bombo, "b" bajo sostenido, "h" hats, "p" pad
    (220 Hz, sin graves). Cada capa cambia el timbre: hay cambios de estructura que no son
    del bajo (los hats y el pad), para ver que B no se queda en ellos."""
    rng = np.random.default_rng(seed)
    trozos = []
    for n, capas in partes:
        t = np.arange(int(n * BAR_S * SR)) / SR
        fase = t % (60.0 / BPM)
        y = np.zeros_like(t)
        if "k" in capas:
            caida = 0.25 if bombo_largo else 0.06
            y += 0.8 * np.exp(-fase / caida) * np.sin(2 * np.pi * 55 * t) * (fase < 0.2)
        if "b" in capas:
            y += 0.3 * np.sin(2 * np.pi * 82 * t)
        if "h" in capas:
            y += 0.25 * rng.standard_normal(t.size) * ((t % (30.0 / BPM)) < 0.02)
        if "p" in capas:
            y += 0.08 * np.sin(2 * np.pi * 220 * t)
        trozos.append(y)
    return np.concatenate(trozos).astype(np.float32)


def compas(cue):
    return cue["positionMs"] / 1000 / BAR_S


def cue(cues, num):
    return next(c for c in cues if c["number"] == num)


# Intro de bombo y hats (cambio en 8, sin bajo), el bajo entra en el 16, break en 48...
CON_INTRO = [(8, "k"), (8, "kh"), (32, "kbh"), (16, "p"), (32, "kbhp"), (16, "kh"), (40, "kbh")]


def test_b_va_donde_entra_el_bajo_y_no_al_primer_cambio():
    y = tema(CON_INTRO)
    cues = worker.detect_cues(y, SR, BPM, 0)
    b = cue(cues, 1)
    assert b["label"] == "BASS-IN"
    assert abs(compas(b) - 16) < 0.02, [round(compas(c), 2) for c in cues]
    assert b["origen"] == "auto"
    assert b["confidence"] >= 0.7   # salto claro: el mezclador lo usa en mezcla completa (>= 0,6)


def test_los_demas_cues_van_despues_de_b_y_en_orden():
    cues = worker.detect_cues(tema(CON_INTRO), SR, BPM, 0)
    posiciones = [c["positionMs"] for c in cues]
    assert posiciones == sorted(posiciones)
    assert [c["number"] for c in cues] == list(range(len(cues)))
    assert all(compas(c) > compas(cue(cues, 1)) for c in cues if c["number"] > 1)
    assert sum(1 for c in cues if c.get("origen")) == 1, "solo B lleva origen"


def test_un_bombo_largo_solo_no_cuenta_como_bajo():
    y = tema(CON_INTRO, bombo_largo=True)
    cues = worker.detect_cues(y, SR, BPM, 0)
    assert abs(compas(cue(cues, 1)) - 16) < 0.02, [round(compas(c), 2) for c in cues]


def test_con_ancla_tarde_b_cae_en_un_downbeat_de_la_rejilla():
    # 300 ms de silencio antes del primer bombo: la rejilla arranca ahí.
    y = np.concatenate([np.zeros(int(0.3 * SR), dtype=np.float32), tema(CON_INTRO)])
    cues = worker.detect_cues(y, SR, BPM, 300)
    b = cue(cues, 1)
    assert abs((b["positionMs"] - 300) / 1000 / BAR_S - 16) < 0.02


def test_si_el_bajo_suena_desde_el_inicio_b_queda_como_antes():
    y = tema([(16, "kb"), (32, "kbh"), (16, "p"), (32, "kbhp"), (16, "kb"), (40, "kbh")])
    assert worker.entrada_del_bajo(y, SR, 0.0, BAR_S * 1000, int(len(y) / SR / BAR_S)) is None
    cues = worker.detect_cues(y, SR, BPM, 0)
    assert cues and not any(c.get("origen") for c in cues)


def test_un_break_sin_bajo_no_es_la_entrada():
    # El bajo está desde el compás 0; vuelve después de un break: eso no es "entrada".
    y = tema([(32, "kbh"), (16, "p"), (48, "kbh"), (40, "kh")])
    assert worker.entrada_del_bajo(y, SR, 0.0, BAR_S * 1000, int(len(y) / SR / BAR_S)) is None


def test_un_tema_sin_bajo_no_tiene_entrada():
    y = tema([(16, "k"), (32, "kh"), (16, "p"), (48, "kh")])
    assert worker.entrada_del_bajo(y, SR, 0.0, BAR_S * 1000, int(len(y) / SR / BAR_S)) is None


def test_entrada_del_bajo_devuelve_el_compas_y_el_salto():
    y = tema(CON_INTRO)
    b, salto = worker.entrada_del_bajo(y, SR, 0.0, BAR_S * 1000, int(len(y) / SR / BAR_S))
    assert b == 16 and salto >= worker.BAJO_SALTO_MIN_DB


def test_si_el_bajo_entra_casi_al_final_b_queda_como_antes():
    # El bajo entra a menos de una frase de la salida: no se fuerza B (quedaría pegado a H).
    y = tema([(8, "k"), (32, "kh"), (16, "p"), (40, "kh"), (24, "kbh")])
    cues = worker.detect_cues(y, SR, BPM, 0)
    assert cues and not any(c.get("origen") for c in cues)
