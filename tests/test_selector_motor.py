"""Selector de motor de separación (v1.23): por defecto TODO sigue en demucs."""
import os
import sys
from pathlib import Path

import numpy as np
import pytest

# El worker exige estas variables al importarse; en pruebas no se usan.
os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stems_worker as sw  # noqa: E402


@pytest.fixture(autouse=True)
def sin_motor_en_entorno(monkeypatch):
    monkeypatch.delenv("STEMS_ENGINE", raising=False)


def test_por_defecto_es_demucs():
    assert sw.MOTOR_DEFECTO == "demucs"
    assert sw.elegir_motor() == "demucs"
    assert sw.elegir_motor({}) == "demucs"
    assert sw.elegir_motor({"model": None}) == "demucs"
    assert sw.elegir_motor({"model": "htdemucs_6s"}) == "demucs"


@pytest.mark.parametrize("valor", ["", "demucs", "DEMUCS", "otro", "híbrido", "1"])
def test_variable_distinta_de_hibrido_sigue_en_demucs(monkeypatch, valor):
    monkeypatch.setenv("STEMS_ENGINE", valor)
    assert sw.elegir_motor({"model": "htdemucs_6s"}) == "demucs"


def test_hibrido_solo_si_se_pide(monkeypatch):
    assert sw.elegir_motor({"model": "hibrido"}) == "hibrido"
    assert sw.elegir_motor({"model": " Hibrido "}) == "hibrido"
    monkeypatch.setenv("STEMS_ENGINE", "hibrido")
    assert sw.elegir_motor({}) == "hibrido"


def test_modelo_demucs_desconocido_vuelve_al_de_siempre():
    assert sw.modelo_demucs({"model": "hibrido"}) == sw.MODEL
    assert sw.modelo_demucs({"model": "cualquiera"}) == sw.MODEL
    assert sw.modelo_demucs({"model": "htdemucs"}) == "htdemucs"


def test_separar_por_defecto_llama_a_demucs(monkeypatch, tmp_path):
    llamadas = []
    monkeypatch.setattr(sw, "run_demucs", lambda src, out, model: llamadas.append(model) or {"drums": Path("d.mp3")})
    monkeypatch.setattr(sw, "separar_hibrido", lambda *a, **k: pytest.fail("no debía usar el híbrido"))
    stems, info = sw.separar(tmp_path / "x.mp3", tmp_path, {"model": None})
    assert llamadas == [sw.MODEL]
    assert info["engine"] == "demucs" and info["model"] == sw.MODEL
    assert sw.MODEL in info["engine_version"]
    assert "engine_fallback" not in info


def test_hibrido_que_falla_cae_en_demucs(monkeypatch, tmp_path):
    def falla(*a, **k):
        raise RuntimeError("sin red")
    monkeypatch.setattr(sw, "separar_hibrido", falla)
    monkeypatch.setattr(sw, "run_demucs", lambda src, out, model: {"drums": Path("d.mp3")})
    stems, info = sw.separar(tmp_path / "x.mp3", tmp_path, {"model": "hibrido"})
    assert info["engine"] == "demucs"
    assert "sin red" in info["engine_fallback"]


def test_hibrido_pedido_reporta_su_motor(monkeypatch, tmp_path):
    monkeypatch.setattr(sw, "separar_hibrido", lambda src, out: {p: out / f"{p}.mp3" for p in
                                                               ("vocals", "drums", "bass", "other", "piano", "guitar")})
    stems, info = sw.separar(tmp_path / "x.mp3", tmp_path, {"model": "hibrido"})
    assert set(stems) == {"vocals", "drums", "bass", "other", "piano", "guitar"}
    assert info["engine"] == "hibrido" and info["model"] == "hibrido"
    assert info["engine_version"].startswith(sw.HIBRIDO_VERSION)


def test_trozos_con_solape_reconstruyen_la_senal():
    torch = pytest.importorskip("torch")
    x = torch.from_numpy(np.random.default_rng(0).standard_normal((2, 50_000)).astype(np.float32))
    for solapes in (1, 2, 4):
        y = sw.separar_por_trozos(lambda b: b, x, trozo=8_000, solapes=solapes, lote=3)
        assert y.shape == (1, 2, 50_000)
        assert torch.allclose(y[0], x, atol=1e-5)
