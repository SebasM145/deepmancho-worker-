"""W6 (auditoría P10): el `result` de analyze() cumple lo que acepta `worker-result`.

Rangos copiados de `supabase/functions/worker-result/index.ts` y `tempo.ts` (repo de la
plataforma). Un campo fuera de rango allá se descarta en silencio: el tema queda sin el dato
y nadie se entera. Si cambias un rango allá, cámbialo aquí."""
import os
import re
import sys

import numpy as np
import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import soundfile as sf  # noqa: E402
import worker  # noqa: E402

SR = 22050
CAMELOT = re.compile(r"^(1[0-2]|[1-9])[AB]$")


def tema(bpm=124.0, ancla_s=0.25, dur_s=64, sr=SR):
    """Bombo con clic, hat en el contratiempo y acorde de La menor (8A)."""
    t = np.arange(int(sr * dur_s)) / sr
    y = np.zeros_like(t)
    rng = np.random.default_rng(1)
    beat = 60.0 / bpm
    for k in np.arange(ancla_s, dur_s, beat):
        i = int(k * sr)
        n = min(int(0.12 * sr), len(y) - i)
        tt = np.arange(n) / sr
        y[i:i + n] += 0.9 * np.sin(2 * np.pi * (50 + 80 * np.exp(-tt * 30)) * tt) * np.exp(-tt * 18)
        m = min(int(0.01 * sr), len(y) - i)
        y[i:i + m] += 0.5 * rng.standard_normal(m)
        j = int((k + beat / 2) * sr)
        m = min(int(0.02 * sr), max(0, len(y) - j))
        y[j:j + m] += 0.2 * rng.standard_normal(m)
    for f in (220.0, 261.63, 329.63):
        y += 0.08 * np.sin(2 * np.pi * f * t)
    return (0.8 * y / np.max(np.abs(y))).astype(np.float32)


@pytest.fixture(scope="module")
def resultado(tmp_path_factory):
    p = tmp_path_factory.mktemp("contrato") / "tema.wav"
    sf.write(p, tema(), SR)
    # Con BPM sembrado, como un tema con etiqueta: el contrato no depende del detector de tempo.
    return worker.analyze(str(p), bpm_seed=124)


def numero(v, lo, hi):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and np.isfinite(v) and lo <= v <= hi


def test_tempo(resultado):
    assert numero(resultado["bpm"], 40, 240)
    assert numero(resultado["bpm_precise"], 40, 240)
    assert numero(resultado["bpm_fine"], -1, 1)
    # Que round(bpm) + bpm_fine == bpm_precise lo prueba tests/test_tempo_coherente.py (PR #10).
    assert resultado["bpm_precise"] == pytest.approx(124, abs=0.05)
    assert resultado["tempo_stability"] in ("constante", "variable", "desconocido")
    assert numero(resultado["tempo_residual_ms"], 0, 10000)


def test_rejilla(resultado):
    v = resultado["first_beat_detected_ms"]
    assert isinstance(v, int) and 0 <= v <= 120000
    # La rejilla cae sobre los bombos (ancla 250 ms, beat 483,9 ms).
    beat = 60000 / 124
    desvio = (v - 250) % beat
    assert min(desvio, beat - desvio) <= 15


def test_tonalidad_y_energia(resultado):
    assert CAMELOT.match(resultado["key"]), resultado["key"]
    for k in ("energy_entry", "energy_peak", "energy_exit"):
        assert isinstance(resultado[k], int) and 1 <= resultado[k] <= 9, k
    assert numero(resultado["analysis_confidence"], 0, 1)
    assert isinstance(resultado["analysis_flags"], (list, dict))


def test_duracion(resultado):
    assert numero(resultado["duration_seconds"], 1, 36000)
    assert abs(resultado["duration_seconds"] - 64) <= 1


def test_cues(resultado):
    cues = resultado["cue_points"]
    assert isinstance(cues, list) and cues
    for c in cues:
        assert isinstance(c["number"], int)
        assert isinstance(c["label"], str) and c["label"]
        assert isinstance(c["positionMs"], (int, float)) and 0 <= c["positionMs"] <= 64000
        assert re.match(r"^#[0-9A-Fa-f]{6}$", c["color"])
    numeros = [c["number"] for c in cues]
    assert len(numeros) == len(set(numeros))


def test_onda(resultado):
    for k in ("waveform_peaks", "waveform_rms"):
        assert isinstance(resultado[k], list) and len(resultado[k]) > 0
        assert all(numero(x, 0, 1.0001) for x in resultado[k]), k
    assert isinstance(resultado["waveform_bands"], dict)
