"""#248: la energía 1-10 se satura. Un tema masterizado suave y uno fuerte salían iguales.

`energy` no cambia (hasta calibrarla con la referencia del dueño): el análisis manda
además `energy_v2`, que sí los distingue. Audio sintético: el mismo groove de bombo y
hi-hat a 124 BPM, a dos niveles de master (≈ −16,5 y ≈ −8,5 LUFS). Medido: de −16,5 a −3,5 LUFS
`energy` da 8 siempre; `energy_v2` va de 5 a 7."""
import os
import sys

import numpy as np
import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import soundfile as sf  # noqa: E402
import worker  # noqa: E402

SR = 44100


def groove(drive, bpm=124, dur_s=30):
    """Bombo, bajo y hi-hat; `drive` es cuánto empuja el limitador (tanh) del master."""
    t = np.arange(int(dur_s * SR)) / SR
    beat = 60.0 / bpm
    fase = t % beat
    bombo = np.sin(2 * np.pi * 55 * fase) * np.exp(-fase * 12)
    bajo = 0.5 * np.sin(2 * np.pi * 110 * t) * (fase > beat / 2)
    f8 = t % (beat / 2)
    hat = np.random.default_rng(1).standard_normal(len(t)) * np.exp(-f8 * 40) * 0.5
    x = np.tanh(drive * (bombo + bajo + hat)) * 0.95
    return np.stack([x, x * 0.9], axis=1).astype(np.float32)


def _medir(tmp_path, nivel):
    p = tmp_path / f"g{nivel}.wav"
    sf.write(p, groove(nivel), SR)
    y11, sr11 = worker.librosa.load(str(p), sr=worker.SR, mono=True)
    rms = worker.librosa.feature.rms(y=y11)[0]
    bands = worker.compute_bands(y11, sr11, worker.BUCKETS)
    return worker.compute_energy(rms, bands), worker.medir_sonoridad(str(p))


def test_la_energia_actual_se_satura_y_la_v2_no(tmp_path):
    e_suave, m_suave = _medir(tmp_path, 0.6)    # ≈ −16,5 LUFS
    e_fuerte, m_fuerte = _medir(tmp_path, 2.0)   # ≈ −8,5 LUFS
    assert m_suave["loudness_lufs"] < m_fuerte["loudness_lufs"] - 6   # de verdad son distintos
    assert e_suave == e_fuerte                                          # #248: hoy salen iguales
    assert m_fuerte["energy_v2"] - m_suave["energy_v2"] >= 2            # la v2 los separa


def test_loudness_lufs_es_bs1770_estereo(tmp_path):
    """#320: un seno de 1 kHz a −20 dBFS en L y R mide −20 LUFS (BS.1770). La escala mono
    de antes daba −23 (y en música real, 3–4 dB menos que cualquier medidor)."""
    t = np.arange(10 * SR) / SR
    x = 0.1 * np.sin(2 * np.pi * 1000 * t)
    p = tmp_path / "seno.wav"
    sf.write(p, np.stack([x, x], axis=1).astype(np.float32), SR)
    assert worker.compute_loudness_lufs(str(p)) == pytest.approx(-20.0, abs=0.2)


def test_loudness_lufs_coincide_con_un_medidor_estandar(tmp_path):
    p = tmp_path / "g.wav"
    sf.write(p, groove(1.0), SR)
    y, _ = worker.librosa.load(str(p), sr=44100, mono=False)
    import pyloudnorm
    estandar = round(float(pyloudnorm.Meter(44100).integrated_loudness(np.ascontiguousarray(y.T))), 2)
    assert worker.compute_loudness_lufs(str(p)) == pytest.approx(estandar, abs=0.02)


def test_sin_audio_no_rompe(tmp_path):
    p = tmp_path / "corto.wav"
    sf.write(p, np.zeros((100, 2), np.float32), SR)
    m = worker.medir_sonoridad(str(p))
    assert "energy_v2" not in m or 1 <= m["energy_v2"] <= 10


def test_el_trabajo_manda_energy_v2_sin_tocar_energy(monkeypatch, tmp_path):
    p = tmp_path / "g.wav"
    sf.write(p, groove(2.0), SR)
    enviados = []
    monkeypatch.setattr(worker, "download_audio", lambda url: str(p))
    monkeypatch.setattr(worker, "send_result", lambda *a, **k: enviados.append((a, k)))
    monkeypatch.setattr(worker, "analyze", lambda path, bpm_seed=None: {"bpm": 124.0, "energy": 8})
    monkeypatch.setattr(worker, "detectar_genero", lambda path: {})
    worker.process_job({"id": "j", "track_id": "t"}, {}, "http://x/a.mp3")
    (args, kw), = enviados
    r = kw.get("result") or args[3]
    assert r["energy"] == 8                    # la de siempre, intacta
    assert 1 <= r["energy_v2"] <= 10 and -7 < r["loudness_lufs"] < -4  # estéreo BS.1770 (en mono daba -8,5)
