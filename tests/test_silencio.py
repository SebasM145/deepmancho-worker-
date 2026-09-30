"""Regresión 30-sep-2026: una pista muda colgaba detect_tempo (0 BPM × 2 para siempre) y trababa la cola."""
import os, sys, time
import numpy as np
import pytest
os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import soundfile as sf  # noqa: E402
import worker  # noqa: E402
from grid_detect import detect_tempo  # noqa: E402


def test_detect_tempo_termina_con_silencio():
    t = time.time()
    bpm, _ = detect_tempo(np.zeros(11025 * 20, dtype=np.float32), 11025)
    assert 60 <= bpm <= 200
    assert time.time() - t < 30


def test_analyze_marca_pista_muda(tmp_path):
    p = tmp_path / "muda.wav"
    sf.write(p, np.zeros(22050 * 10, dtype=np.float32), 22050)
    with pytest.raises(worker.AudioMudo):
        worker.analyze(str(p))
