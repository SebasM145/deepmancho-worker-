"""Noche del 11-oct-2026: casos borde del analizador v8 (F1, mezcla y energy_v2) que no tenían prueba.

Silencio, mono, audio corto y formas raras del arreglo: el bloque `mezcla` tiene que salir sin
NaN ni infinitos (va a JSON) y `energia_v2` siempre entre 1 y 10 o vacía.
"""
import json
import math
import os
import sys

import numpy as np
import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import analizador_v8 as v8  # noqa: E402

SR = 44100


def tono(hz, dur_s, amp=0.3):
    return amp * np.sin(2 * np.pi * hz * np.arange(int(dur_s * SR)) / SR)


def sin_nan_ni_infinitos(bloque):
    texto = json.dumps(bloque, allow_nan=False)  # falla si hay NaN o ±inf
    return bool(texto)


# ── _r y _estereo ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("x, esperado", [(1.234, 1.23), (None, None), (float("nan"), None),
                                          (float("inf"), None), (-math.inf, None), (np.float32(2.5), 2.5)])
def test_r_redondea_y_descarta_lo_no_finito(x, esperado):
    assert v8._r(x) == esperado


@pytest.mark.parametrize("forma", [(100,), (100, 1), (100, 2), (100, 3)])
def test_estereo_siempre_dos_canales(forma):
    a = v8._estereo(np.ones(forma, dtype=np.float32))
    assert a.shape == (100, 2)
    assert a.dtype == np.float64


def test_estereo_de_mono_copia_el_canal():
    a = v8._estereo(np.arange(5.0))
    assert (a[:, 0] == a[:, 1]).all()


# ── medidas_mezcla ───────────────────────────────────────────────────────────
def test_mezcla_de_silencio_no_rompe_y_queda_vacia():
    m = v8.medidas_mezcla(np.zeros(5 * SR), SR)
    assert sin_nan_ni_infinitos(m)
    assert m["lufs_integrado"] is None
    assert m["true_peak_dbtp"] is None
    assert m["lra_lu"] is None
    assert m["crest_db"] is None
    assert m["ancho_estereo"] == 0.0
    assert m["correlacion_lr"] == 1.0
    assert v8.energia_v2(m, 5.0) is None


def test_mezcla_mono_es_ancho_cero_y_correlacion_uno():
    m = v8.medidas_mezcla(tono(1000, 5), SR)
    assert sin_nan_ni_infinitos(m)
    assert m["ancho_estereo"] == 0.0
    assert m["correlacion_lr"] == 1.0
    assert m["crest_db"] == pytest.approx(3.0, abs=0.1)  # seno: pico/RMS = √2
    assert m["lra_lu"] == pytest.approx(0.0, abs=0.1)    # volumen constante


def test_mezcla_de_menos_de_un_segundo_sin_tercios_ni_lra():
    ruido = 0.3 * np.random.RandomState(248).randn(SR // 2)
    m = v8.medidas_mezcla(ruido, SR)
    assert sin_nan_ni_infinitos(m)
    assert m["tercios_db"] == {}
    assert m["lra_lu"] is None
    assert m["lufs_integrado"] is not None


# ── tercios_de_octava ────────────────────────────────────────────────────────
def test_tercios_de_silencio_quedan_en_el_piso():
    t = v8.tercios_de_octava(np.zeros(2 * SR), SR)
    assert len(t) == len(v8.TERCIOS_HZ)
    assert set(t.values()) == {-120.0}


def test_tercios_a_tasa_baja_se_cortan_en_nyquist():
    sr = 16000
    t = v8.tercios_de_octava(np.random.RandomState(1).randn(2 * sr), sr)
    assert t
    assert all(float(k) * 2 ** (1 / 6) <= sr / 2 for k in t)
    assert "8000" not in t and "16000" not in t
    assert max(t.values()) == 0.0


# ── lra_lu ───────────────────────────────────────────────────────────────────
def test_lra_de_audio_corto_es_vacio():
    assert v8.lra_lu(tono(1000, 2.5), SR) is None


def test_lra_ignora_el_silencio_por_la_puerta_absoluta():
    # Los bloques mudos (−inf) quedan fuera; solo cuentan los que tocan el borde del silencio.
    a = np.concatenate([tono(1000, 10), np.zeros(10 * SR), tono(1000, 10)])
    lra = v8.lra_lu(a, SR)
    assert lra is not None and math.isfinite(lra)
    assert 0.0 <= lra < 3.0


# ── energia_v2 ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("mezcla, golpes, esperado", [
    ({"lufs_integrado": -3.0, "tercios_db": {"4000": 0.0}}, 20.0, 10),    # todo arriba del tope
    ({"lufs_integrado": -40.0, "tercios_db": {"100": 0.0}}, 0.1, 1),      # todo debajo del piso
    ({"lufs_integrado": None, "tercios_db": {"4000": 0.0}}, 6.0, None),   # sin sonoridad no se inventa
    ({"lufs_integrado": -9.5}, None, 3),                                  # sin tercios ni golpes: solo el volumen (1 + 9·0,25)
])
def test_energia_v2_en_los_bordes(mezcla, golpes, esperado):
    assert v8.energia_v2(mezcla, golpes) == esperado


def test_energia_v2_tercio_vacio_no_rompe():
    assert v8.energia_v2({"lufs_integrado": -9.0, "tercios_db": {"100": None, "4000": -3.0}}, 5.0) in range(1, 11)


def test_energia_v2_crece_con_cada_componente():
    base = {"lufs_integrado": -10.0, "tercios_db": {"100": 0.0, "4000": -12.0}}
    e = v8.energia_v2(base, 5.0)
    assert v8.energia_v2({**base, "lufs_integrado": -7.0}, 5.0) > e
    assert v8.energia_v2({**base, "tercios_db": {"100": 0.0, "4000": 0.0}}, 5.0) > e
    assert v8.energia_v2(base, 8.0) > e


@pytest.mark.parametrize("x, esperado", [(-20, 0.0), (-13, 0.0), (-9.5, 0.5), (-6, 1.0), (0, 1.0)])
def test_escala_recorta_entre_0_y_1(x, esperado):
    assert v8._escala(x, v8.V2_LUFS) == pytest.approx(esperado)
