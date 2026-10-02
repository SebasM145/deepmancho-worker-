"""Tempo efectivo coherente con worker-result (2-oct-2026).

worker-result (`supabase/functions/worker-result/tempo.ts`, plataforma) guarda:
- BPM libre (sin etiqueta): bpm = Math.round(bpm) y bpm_fine tal cual → tempo = entero + fino.
- BPM bloqueado (metadata, manual o bpm_tag; es el único caso en que worker-next
  manda semilla): no toca `bpm` y usa bpm_precise. Además compara `bpm` del worker
  con la etiqueta para levantar `bpm_etiqueta_difiere`, así que con semilla `bpm`
  tiene que ser la medida propia de detect_grid y no el entero de la semilla.

Aquí se replica esa lógica (lo justo) para probar el contrato desde este lado."""
import math
import os
import sys

import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import soundfile as sf  # noqa: E402
import worker  # noqa: E402
from test_tempo_sin_semilla import tema  # noqa: E402


def js_round(x):
    return math.floor(x + 0.5)


def decidir_tempo(existente, medido):
    """Copia mínima de decidirTempo (tempo.ts) + la bandera de worker-result/index.ts."""
    bloqueado = existente.get("bpm_source") in ("metadata", "manual") or existente.get("bpm_tag") is not None
    if not bloqueado:
        return {"bpm": js_round(medido["bpm"]), "bpm_fine": round(medido["bpm_fine"], 3), "bandera": None}
    fijo = existente["bpm"]
    up = {"bpm": fijo, "bpm_fine": existente.get("bpm_fine", 0), "bandera": None}
    tempo = medido.get("bpm_precise")
    if tempo is not None and abs(tempo - fijo) < 0.5:
        up["bpm_fine"] = round(tempo - fijo, 3)
    b = medido["bpm"]
    cerca = lambda a, c: abs(a - c) <= max(1, c * 0.01)
    if not cerca(b, fijo) and not cerca(b * 2, fijo) and not cerca(b / 2, fijo):
        up["bandera"] = f"bpm_etiqueta_difiere:{fijo}→{round(b, 2)}"
    return up


def _analizar(tmp_path, monkeypatch, bpm_real, bpm_grid, semilla):
    monkeypatch.setattr(worker, "detect_grid", lambda y, sr, seed_bpm=None: (bpm_grid, 250))
    p = tmp_path / "tema.wav"
    sf.write(p, tema(bpm_real, dur_s=64, sr=22050), 22050)
    return worker.analyze(str(p), bpm_seed=semilla)


@pytest.mark.parametrize("bpm_grid", [124.3, 123.6])
def test_sin_semilla_el_tempo_guardado_es_el_afinado(tmp_path, monkeypatch, bpm_grid):
    r = _analizar(tmp_path, monkeypatch, 124, bpm_grid, None)
    g = decidir_tempo({"bpm_source": None}, r)
    assert g["bpm"] + g["bpm_fine"] == pytest.approx(r["bpm_precise"], abs=0.01)
    assert r["bpm"] == bpm_grid  # bpm_detected conserva la medida con decimales


def test_sin_semilla_con_medio_exacto_redondea_como_js(tmp_path, monkeypatch):
    # round(124.5) de Python da 124; Math.round de JS da 125. bpm_fine tiene que ir contra 125.
    r = _analizar(tmp_path, monkeypatch, 124, 124.5, None)
    g = decidir_tempo({"bpm_source": None}, r)
    assert g["bpm"] == 125
    assert g["bpm"] + g["bpm_fine"] == pytest.approx(r["bpm_precise"], abs=0.01)


def test_con_etiqueta_bpm_es_la_medida_y_la_bandera_puede_saltar(tmp_path, monkeypatch):
    # Etiqueta 124, detect_grid mide 131: la bandera tiene que poder avisar al DJ.
    r = _analizar(tmp_path, monkeypatch, 124, 131.0, 124)
    assert r["bpm"] == 131.0
    g = decidir_tempo({"bpm": 124, "bpm_source": "metadata"}, r)
    assert g["bandera"] == "bpm_etiqueta_difiere:124→131.0"
    assert g["bpm"] == 124  # el entero bloqueado no se toca
    assert g["bpm"] + g["bpm_fine"] == pytest.approx(r["bpm_precise"], abs=0.01)
    assert r["bpm_precise"] == pytest.approx(124, abs=0.05)


def test_con_etiqueta_y_medida_coherente_no_hay_bandera(tmp_path, monkeypatch):
    r = _analizar(tmp_path, monkeypatch, 124, 124.2, 124)
    g = decidir_tempo({"bpm": 124, "bpm_tag": 124}, r)
    assert g["bandera"] is None
    assert g["bpm"] + g["bpm_fine"] == pytest.approx(124, abs=0.05)


@pytest.mark.parametrize("x,esperado", [(124.5, 125), (124.49, 124), (125.5, 126), (-0.5, 0)])
def test_entero_js(x, esperado):
    assert worker.entero_js(x) == esperado
