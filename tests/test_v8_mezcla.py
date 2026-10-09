"""v8 F1 · medidas de mezcla con señales de respuesta conocida (BS.1770 / EBU Tech 3342)."""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import analizador_v8 as v8  # noqa: E402

SR = 48000


def tono(hz, amp, seg, fase=0.0):
    t = np.arange(int(seg * SR)) / SR
    x = amp * np.sin(2 * np.pi * hz * t + fase)
    return np.stack([x, x], axis=1)


def test_lufs_de_un_tono_a_menos_20():
    m = v8.medidas_mezcla(tono(1000, 0.1, 10), SR)       # -20 dBFS en L y R → -20 LUFS
    assert m["lufs_integrado"] == pytest.approx(-20.0, abs=0.2)
    assert m["crest_db"] == pytest.approx(3.0, abs=0.1)     # seno: pico/RMS = √2
    assert m["ancho_estereo"] == pytest.approx(0.0, abs=1e-3)  # mono
    assert m["correlacion_lr"] == pytest.approx(1.0, abs=1e-3)


def test_true_peak_ve_el_pico_entre_muestras():
    # fs/4 con fase de 45°: las muestras caen en 0,707·A, el pico real es A (−6,02 dBTP).
    x = tono(SR / 4, 0.5, 2, fase=np.pi / 4)
    assert 20 * np.log10(np.max(np.abs(x))) == pytest.approx(-9.03, abs=0.05)
    assert v8.true_peak_db(x, SR) == pytest.approx(-6.02, abs=0.3)


def test_lra_caso_1_de_la_ebu():
    # Tech 3342, caso 1: 20 s a −20 LUFS y 20 s a −30 LUFS → LRA 10 ± 1 LU.
    x = np.vstack([tono(1000, 0.1, 20), tono(1000, 0.0316, 20)])
    assert v8.lra_lu(x, SR) == pytest.approx(10.0, abs=1.0)


def test_ancho_estereo_con_lados_opuestos():
    x = tono(500, 0.1, 3)
    x[:, 1] *= -1                                             # L = −R: todo es lado
    m = v8.medidas_mezcla(x, SR)
    assert m["ancho_estereo"] == pytest.approx(1.0, abs=1e-3)
    assert m["correlacion_lr"] == pytest.approx(-1.0, abs=1e-3)


def test_tercios_marcan_la_banda_del_tono():
    t = v8.medidas_mezcla(tono(1000, 0.1, 5), SR)["tercios_db"]
    assert t["1000"] == 0.0
    assert t["100"] < -40 and t["10000"] < -40


def test_energia_v2_distingue_volumen():
    suave = v8.medidas_mezcla(tono(1000, 0.05, 10), SR)       # ≈ −26 LUFS
    fuerte = v8.medidas_mezcla(tono(1000, 0.4, 10), SR)       # ≈ −8 LUFS
    assert v8.energia_v2(suave, 2.0) < v8.energia_v2(fuerte, 2.0)
    assert v8.energia_v2({"lufs_integrado": None}) is None



# #248: medidas reales de 40 temas al azar del catálogo del dueño (9-oct), solo números:
# (LUFS integrado, fracción de energía desde 2 kHz, golpes por segundo).
CATALOGO_248 = [
    (-9.79, 0.0297, 6.635),
    (-9.03, 0.0674, 6.017),
    (-8.95, 0.0651, 4.917),
    (-7.25, 0.0712, 4.52),
    (-7.12, 0.0483, 6.814),
    (-9.29, 0.0224, 6.744),
    (-7.9, 0.1739, 6.491),
    (-7.89, 0.0627, 5.754),
    (-11.71, 0.0358, 3.493),
    (-8.66, 0.0356, 7.394),
    (-8.71, 0.0358, 7.247),
    (-11.47, 0.0448, 6.922),
    (-9.03, 0.0766, 8.179),
    (-9.96, 0.103, 4.617),
    (-8.99, 0.0634, 6.183),
    (-7.07, 0.0953, 5.612),
    (-8.52, 0.0584, 7.116),
    (-10.72, 0.0307, 6.822),
    (-9.06, 0.0419, 5.137),
    (-8.24, 0.0603, 5.179),
    (-8.54, 0.0715, 5.211),
    (-7.8, 0.0366, 7.126),
    (-9.11, 0.0751, 7.456),
    (-10.28, 0.0865, 6.969),
    (-6.26, 0.0396, 5.394),
    (-8.85, 0.1249, 6.475),
    (-10.97, 0.0447, 5.422),
    (-7.35, 0.1076, 4.795),
    (-8.55, 0.1183, 4.397),
    (-8.88, 0.0586, 6.863),
    (-9.21, 0.0555, 5.478),
    (-9.02, 0.0264, 6.669),
    (-9.88, 0.0425, 6.084),
    (-10.37, 0.0832, 5.757),
    (-9.75, 0.042, 4.024),
    (-9.07, 0.0524, 5.677),
    (-10.79, 0.0446, 6.262),
    (-9.65, 0.0402, 5.469),
    (-9.45, 0.0503, 5.228),
    (-10.09, 0.0396, 6.62),
]


def _mezcla(lufs, agudos_frac):
    import math
    return {"lufs_integrado": lufs,
            "tercios_db": {"100": 10 * math.log10(1 - agudos_frac), "4000": 10 * math.log10(agudos_frac)}}


def test_energia_v2_reparte_el_catalogo_real():
    """Con las escalas de antes, el 70 % salía en 7 (el componente de golpes topado en el 52 %)."""
    from collections import Counter
    vals = [v8.energia_v2(_mezcla(lufs, ag), golpes) for lufs, ag, golpes in CATALOGO_248]
    cuenta = Counter(vals)
    assert max(cuenta.values()) <= len(vals) * 0.5, cuenta
    assert len(cuenta) >= 5, cuenta


def test_energia_v2_golpes_ya_no_se_topan():
    """6 y 7 golpes por segundo (la mediana del catálogo) ya no valen lo mismo."""
    m = _mezcla(-9.0, 0.05)
    a = [v8.energia_v2(m, g) for g in (3.0, 5.0, 7.0, 9.0)]
    assert a == sorted(a) and a[0] < a[-1], a
    assert v8._escala(6.0, v8.V2_GOLPES) < v8._escala(7.0, v8.V2_GOLPES) < 1.0
