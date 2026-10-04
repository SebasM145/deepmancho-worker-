"""Carga de Germán (4-oct-2026): dos casos reales.
1) «Gratitude» (631 s) mataba la réplica 3 veces: la estimación de tempo de beat_track armaba
   un tempograma de 384 × ~100.000 cuadros (+6,6 GB). tempo_por_bloques da el mismo tempo.
2) «Paris» traía TBPM=240: con el BPM bloqueado, se analiza en su octava (120) y se avisa."""
import os
import sys

import librosa
import numpy as np
import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import worker  # noqa: E402
from test_tempo_sin_semilla import tema  # noqa: E402

SR = 22050


@pytest.mark.parametrize("bpm", [98, 122, 128, 174])
def test_tempo_por_bloques_igual_a_librosa(bpm):
    env = librosa.onset.onset_strength(y=tema(bpm, dur_s=60, sr=SR).astype(np.float32), sr=SR, hop_length=128)
    original = librosa.feature.tempo(onset_envelope=env, sr=SR, hop_length=128, start_bpm=float(bpm))
    for bloque in (4096, 333, 50):           # varios bloques: el resultado no depende del corte
        assert worker.tempo_por_bloques(env, SR, 128, float(bpm), bloque=bloque)[0] == pytest.approx(original[0], rel=1e-9)


def test_beats_iguales_a_beat_track_completo():
    env = librosa.onset.onset_strength(y=tema(124, dur_s=60, sr=SR).astype(np.float32), sr=SR, hop_length=128)
    _, antes = librosa.beat.beat_track(onset_envelope=env, sr=SR, hop_length=128, trim=False, start_bpm=124.0, tightness=200)
    t = worker.tempo_por_bloques(env, SR, 128, 124.0)
    _, ahora = librosa.beat.beat_track(onset_envelope=env, sr=SR, hop_length=128, trim=False, bpm=t, tightness=200)
    assert np.array_equal(antes, ahora)


@pytest.mark.parametrize("entra,sale", [(240, 120), (248, 124), (60, 120), (87, 87), (174, 174), (122, 122), (360, 90)])
def test_bpm_en_rango(entra, sale):
    assert worker.bpm_en_rango(entra) == pytest.approx(sale)


def test_bpm_en_rango_invalido():
    assert worker.bpm_en_rango(None) is None and worker.bpm_en_rango(0) is None and worker.bpm_en_rango("x") is None


def test_etiqueta_240_se_analiza_a_120_y_avisa(monkeypatch, tmp_path):
    p = tmp_path / "a.mp3"
    p.write_bytes(b"x")
    semillas, enviados = [], []
    monkeypatch.setattr(worker, "download_audio", lambda url: str(p))
    monkeypatch.setattr(worker, "analyze", lambda path, bpm_seed=None: semillas.append(bpm_seed) or {"energy": 7, "analysis_flags": ["tempo_variable"]})
    monkeypatch.setattr(worker, "detectar_genero", lambda path: {})
    monkeypatch.setattr(worker, "medir_sonoridad", lambda path: {})
    monkeypatch.setattr(worker, "send_result", lambda *a, **k: enviados.append(k.get("result")))
    worker.process_job({"id": "j", "track_id": "t"}, {"bpm": 240, "artist": "a", "title": "b"}, "http://x/a.mp3")
    assert semillas == [120.0]
    assert enviados[0]["analysis_flags"] == ["tempo_variable", "bpm_etiqueta_octava:240→120"]


def test_etiqueta_en_rango_no_cambia(monkeypatch, tmp_path):
    p = tmp_path / "a.mp3"
    p.write_bytes(b"x")
    semillas, enviados = [], []
    monkeypatch.setattr(worker, "download_audio", lambda url: str(p))
    monkeypatch.setattr(worker, "analyze", lambda path, bpm_seed=None: semillas.append(bpm_seed) or {"energy": 7})
    monkeypatch.setattr(worker, "detectar_genero", lambda path: {})
    monkeypatch.setattr(worker, "medir_sonoridad", lambda path: {})
    monkeypatch.setattr(worker, "send_result", lambda *a, **k: enviados.append(k.get("result")))
    worker.process_job({"id": "j", "track_id": "t"}, {"bpm": 122, "artist": "a", "title": "b"}, "http://x/a.mp3")
    assert semillas == [122]
    assert "analysis_flags" not in enviados[0]
