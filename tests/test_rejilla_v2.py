"""7.6.25 · Analizador v2 del «1», en sombra (rejilla_v2.py). Sin torch: aquí se prueba lo puro.

`tests/datos/rejilla_v2_banco.json` trae beats y downbeats SINTÉTICOS y el bloque que calcula con
ellos el banco de dj-connect (`tools/analizador`: rejilla.ts + arbitro.ts + sombra.ts, con el
árbitro `small0.json`). El worker tiene que dar exactamente lo mismo: es el mismo cálculo en otro
lenguaje. Con la música real se comparó fuera del repo: 904 de 904 bloques iguales (CHANGELOG).
"""
import json
import os

import pytest

import rejilla_v2 as r2

BANCO = json.load(open(os.path.join(os.path.dirname(__file__), "datos", "rejilla_v2_banco.json"), encoding="utf-8"))
CAMPOS_DEL_BANCO = ("bpm", "ancla_ms", "regla", "acuerdo", "confianza", "intro_s", "version")


def test_la_version_es_la_del_banco():
    assert r2.version_de() == BANCO["version"] == "0b0acffdb089"


@pytest.mark.parametrize("nombre", sorted(BANCO["casos"]))
def test_el_bloque_es_el_del_banco(nombre):
    caso = BANCO["casos"][nombre]
    r = r2.rejilla_global(caso["beats"], caso["downbeats"])
    if caso["banco"] is None:
        assert r is None
        return
    bloque = r2.bloque_v2(r)
    assert {k: bloque[k] for k in CAMPOS_DEL_BANCO} == caso["banco"]


def test_el_bloque_trae_lo_que_acepta_worker_result():
    caso = BANCO["casos"]["temblor_hueco_y_doble_tiempo"]
    b = r2.bloque_v2(r2.rejilla_global(caso["beats"], caso["downbeats"]))
    assert b["v"] == 1 and b["modelo"] == "small0" and b["regla"] in ("voto", "primer_beat")
    assert 40 < b["bpm"] < 240 and 40 < b["bpm_fino"] < 240 and -1000 <= b["ancla_ms"] <= 120000
    assert 0 <= b["acuerdo"] <= 1 and 0.5 <= b["confianza"] <= 1 and b["intro_s"] >= 0
    assert b["fase_uno"] in (0, 1, 2, 3) and len(b["votos"]) == 4 and all(isinstance(v, int) for v in b["votos"])
    assert 0 <= b["descartados"] <= 1 and b["residuo_ms"] >= 0 and b["n_beats"] > 0
    json.dumps(b)   # todo serializable


@pytest.mark.parametrize("bpm,esperado", [(125.04, 125), (124.91, 125), (127.97, 128), (130.006, 130), (124.5, None), (125.2, None)])
def test_r2_bpm_entero_solo_a_una_decima(bpm, esperado):
    assert r2.bpm_redondo(bpm) == esperado


def test_el_sesgo_medido_se_resta_al_ancla():
    caso = BANCO["casos"]["limpio_126"]            # primer beat y primer «1» en 80 ms
    b = r2.bloque_v2(r2.rejilla_global(caso["beats"], caso["downbeats"]))
    assert b["ancla_ms"] == pytest.approx(80 - r2.ARBITRO_SMALL0["sesgoMs"], abs=0.2)
    assert b["bpm"] == 126 and b["bpm_redondo"] == 126


def test_sombra_sin_rejilla_devuelve_un_bloque_con_error(monkeypatch):
    monkeypatch.setattr(r2, "beats_del_modelo", lambda path, modelo, hilos: ([0.5, 1.0], [0.5], 12.0))
    b = r2.sombra("x.mp3", hilos=2)
    assert b["error"] == "sin_rejilla" and b["v"] == 1 and b["version"] == r2.version_de() and b["dur_s"] == 12.0
    assert "bpm" not in b and "ancla_ms" not in b


def test_sombra_arma_el_bloque_con_tiempo_y_memoria(monkeypatch):
    caso = BANCO["casos"]["rapido_174"]
    monkeypatch.setattr(r2, "beats_del_modelo", lambda path, modelo, hilos: (caso["beats"], caso["downbeats"], 345.0))
    b = r2.sombra("x.mp3", hilos=3)
    assert {k: b[k] for k in CAMPOS_DEL_BANCO} == caso["banco"]
    assert b["hilos"] == 3 and b["seg"] >= 0 and b["mem_mb"] > 0 and b["dur_s"] == 345.0
