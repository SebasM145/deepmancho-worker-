"""calidad_pistas: qué tan limpia salió cada pista separada y de quién trae filtración."""
import os
import sys

import numpy as np
import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stems_worker as sw  # noqa: E402

SR = 22050


def pulsos(dur_s=8.0, cada_s=0.5, amp=0.8):
    """Ráfagas cortas a tempo: una batería que sube y baja."""
    y = np.zeros(int(dur_s * SR), dtype=np.float32)
    n = int(0.05 * SR)
    ruido = np.random.RandomState(0).randn(n).astype(np.float32) * amp
    for i in range(0, len(y) - n, int(cada_s * SR)):
        y[i:i + n] += ruido
    return y


def sostenido(dur_s=8.0, amp=0.1):
    t = np.arange(int(dur_s * SR)) / SR
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


@pytest.fixture
def pistas(monkeypatch):
    audio = {}
    monkeypatch.setattr(sw, "to_wav_mono", lambda path, sr=SR: (audio[path], SR))
    return audio


def test_pista_casi_vacia_sale_como_vacia_y_no_desaparece(pistas):
    pistas["drums"] = pulsos()
    pistas["vocals"] = np.zeros(100, dtype=np.float32)   # < 80 ms: menos de 4 ventanas
    out = sw.calidad_pistas({"drums": "drums", "vocals": "vocals"})
    assert set(out) == {"drums", "vocals"}
    assert out["vocals"] == {"estado": "vacia", "de": None, "correlacion": 0.0}


def test_envolvente_corta_devuelve_tres_salidas():
    env, nivel, fluct = sw._envolvente(np.zeros(10, dtype=np.float32), SR)
    assert len(env) == 4 and nivel == 0.0 and fluct == 0.0


def test_silencio_largo_es_vacia(pistas):
    pistas["bass"] = np.zeros(SR * 4, dtype=np.float32)
    assert sw.calidad_pistas({"bass": "bass"})["bass"]["estado"] == "vacia"


def test_la_filtracion_va_del_fuerte_al_debil(pistas):
    bateria = pulsos(amp=0.8)
    pistas["drums"] = bateria
    pistas["piano"] = sostenido() * 0.3 + bateria * 0.3   # el piano trae batería encima
    pistas["bass"] = sostenido(amp=0.05)                  # sostenido y limpio
    out = sw.calidad_pistas({k: k for k in pistas})
    assert out["piano"]["estado"] in ("con_algo", "mezclada") and out["piano"]["de"] == "drums"
    assert out["drums"]["estado"] == "limpia"             # la más fuerte no trae al piano
    assert out["bass"]["estado"] == "limpia" and out["bass"]["de"] is None


def test_pista_que_no_se_puede_leer_se_omite(pistas, monkeypatch):
    def leer(path, sr=SR):
        if path == "rota":
            raise RuntimeError("ffmpeg falló")
        return pulsos(), SR
    monkeypatch.setattr(sw, "to_wav_mono", leer)
    out = sw.calidad_pistas({"drums": "ok", "other": "rota"})
    assert set(out) == {"drums"}
