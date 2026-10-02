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
