"""Noche del 11-oct-2026: afinación, croma y tonalidad de los stems (v1.21), que no tenían prueba.

`afinacion_cents` y `tonalidad_de` llenan `qc.afinacion_cents`, `qc.key_detectada` y `qc.key_conf`
de cada loop. Con acordes sintéticos de afinación conocida: sin red, ffmpeg ni demucs.
"""
import os
import sys

import numpy as np
import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stems_worker as sw  # noqa: E402

SR = 22050

# Escala y tríada de cada tonalidad (notas MIDI), con la tónica en el grave.
DO_MAYOR = [48, 52, 55, 60, 64, 67, 62, 65, 69, 71]
LA_MENOR = [45, 48, 52, 57, 60, 64, 59, 62, 65, 67]
SOL_MAYOR = [43, 47, 50, 55, 59, 62, 57, 60, 64, 66]
MI_MENOR = [40, 43, 47, 52, 55, 59, 54, 57, 60, 62]


def tono(hz, dur_s=4.0, amp=0.3):
    t = np.arange(int(dur_s * SR)) / SR
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def nota_hz(midi, cents=0.0):
    return 440.0 * 2 ** ((midi - 69) / 12 + cents / 1200)


def acorde(notas, cents=0.0):
    return sum(tono(nota_hz(m, cents)) for m in notas)


# ── afinacion_cents ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("cents", [0, 20, -30, 45, -45])
def test_afinacion_mide_el_desvio_respecto_de_la_440(cents):
    assert sw.afinacion_cents(acorde([57, 64, 69, 73], cents), SR) == pytest.approx(cents, abs=1.0)


def test_afinacion_queda_entre_menos_50_y_50():
    # 60 cents arriba es lo mismo que 40 abajo de la nota siguiente.
    assert sw.afinacion_cents(acorde([57, 64, 69], 60), SR) == pytest.approx(-40, abs=1.0)


@pytest.mark.parametrize("y", [np.zeros(100, dtype=np.float32), np.zeros(2 * SR, dtype=np.float32)],
                         ids=["mas_corto_que_la_ventana", "silencio"])
def test_afinacion_sin_material_no_inventa(y):
    assert sw.afinacion_cents(y, SR) is None


# ── croma ────────────────────────────────────────────────────────────────────
def test_croma_de_un_la_cae_en_su_clase():
    c = sw.croma(tono(440.0), SR)
    assert c.shape == (12,)
    assert int(np.argmax(c)) == 9  # La = 9 contando desde Do
    assert c.sum() == pytest.approx(1.0)


def test_croma_usa_la_referencia_dada():
    # Un La desafinado 48 cents abajo: con la referencia de 440 cae a medio camino, con la medida cae en La.
    y = tono(nota_hz(69, -48))
    c = sw.croma(y, SR, ref_hz=440.0 * 2 ** (-48 / 1200))
    assert int(np.argmax(c)) == 9
    assert c[9] > 0.9


def test_croma_corto_es_cero():
    assert not sw.croma(np.zeros(100, dtype=np.float32), SR).any()


# ── tonalidad_de ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("notas, camelot", [(DO_MAYOR, "8B"), (LA_MENOR, "8A"), (SOL_MAYOR, "9B"), (MI_MENOR, "9A")])
def test_tonalidad_en_camelot(notas, camelot):
    key, conf = sw.tonalidad_de(acorde(notas), SR)
    assert key == camelot
    assert 0 < conf <= 1


def test_tonalidad_con_la_afinacion_medida_no_cae_en_la_vecina():
    """v1.21: un tema 48 cents abajo, leído con La = 440, se va a otra tonalidad; con la afinación medida, no."""
    y = acorde(DO_MAYOR, -48)
    cents = sw.afinacion_cents(y, SR)
    assert cents == pytest.approx(-48, abs=1.0)
    assert sw.tonalidad_de(y, SR)[0] != "8B"
    assert sw.tonalidad_de(y, SR, cents)[0] == "8B"


def test_tonalidad_sin_cents_es_igual_que_con_cero():
    y = acorde(SOL_MAYOR)
    assert sw.tonalidad_de(y, SR, None) == sw.tonalidad_de(y, SR, 0.0)


def test_tonalidad_de_silencio():
    assert sw.tonalidad_de(np.zeros(2 * SR, dtype=np.float32), SR) == (None, 0.0)


def test_rueda_camelot_completa_y_relativas_con_el_mismo_numero():
    assert sorted(sw._CAMELOT_MAYOR) == list(range(12))
    assert sorted(sw._CAMELOT_MENOR) == list(range(12))
    assert len(set(sw._CAMELOT_MAYOR.values())) == len(set(sw._CAMELOT_MENOR.values())) == 12
    for tonica_mayor, codigo in sw._CAMELOT_MAYOR.items():
        relativa_menor = (tonica_mayor + 9) % 12  # Do mayor ↔ La menor
        assert sw._CAMELOT_MENOR[relativa_menor] == codigo[:-1] + "A"
    for tonica, codigo in sw._CAMELOT_MAYOR.items():
        quinta = (tonica + 7) % 12  # una quinta arriba = un número más en la rueda
        assert int(sw._CAMELOT_MAYOR[quinta][:-1]) == int(codigo[:-1]) % 12 + 1


# ── densidad_notas ───────────────────────────────────────────────────────────
def test_densidad_notas_cuenta_golpes_por_compas():
    y = np.zeros(10 * SR, dtype=np.float32)
    y[SR // 4::SR // 2] = 1.0  # un golpe por tiempo a 120 BPM
    assert sw.densidad_notas(y, SR, 120, 0, 1, largo=2) == pytest.approx(4.0)
    assert sw.densidad_notas(y, SR, 120, 0, 2, largo=2) == pytest.approx(4.0)
    assert sw.densidad_notas(np.zeros(10 * SR, dtype=np.float32), SR, 120, 0, 1, largo=2) == 0.0


# ── info_motor / hibrido_dir ─────────────────────────────────────────────────
def test_info_motor_demucs():
    info = sw.info_motor("demucs", "htdemucs_6s")
    assert info["engine"] == "demucs" and info["model"] == "htdemucs_6s"
    assert info["engine_version"].startswith("demucs ") and info["engine_version"].endswith("· htdemucs_6s")


def test_info_motor_hibrido_dice_versiones_y_commit(monkeypatch):
    monkeypatch.delenv("HIBRIDO_OTHER", raising=False)
    info = sw.info_motor("hibrido", "lo-que-sea")
    assert info["engine"] == "hibrido" and info["model"] == "hibrido"
    assert sw.HIBRIDO_VERSION in info["engine_version"]
    assert f"msst {sw.MSST_COMMIT[:7]}" in info["engine_version"]
    assert info["engine_version"].endswith("other=resto")


def test_version_de_paquete_que_no_existe():
    assert sw._version_de("paquete-que-no-existe-dj-connect") == "?"


def test_hibrido_dir_respeta_las_variables(monkeypatch, tmp_path):
    monkeypatch.setenv("HIBRIDO_DIR", str(tmp_path / "h"))
    assert sw.hibrido_dir() == tmp_path / "h"
    monkeypatch.delenv("HIBRIDO_DIR")
    monkeypatch.setenv("TORCH_HOME", str(tmp_path / "torch"))
    assert sw.hibrido_dir() == tmp_path / "torch" / "hibrido"
