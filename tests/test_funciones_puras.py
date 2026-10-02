"""W6 (auditoría P10): funciones puras de stems y del verificador con audio sintético.
Sin red, sin torch y sin demucs; lo que necesita ffmpeg se salta si no está instalado."""
import os
import shutil
import sys

import numpy as np
import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import grid_verifier as gv  # noqa: E402
import stems_worker as sw  # noqa: E402

SR = 22050


def bombos(bpm, ancla_s=0.25, dur_s=32, sr=SR):
    """Solo bombo (barrido 130 → 50 Hz) en cada negra desde el ancla."""
    y = np.zeros(int(sr * dur_s))
    for k in np.arange(ancla_s, dur_s, 60.0 / bpm):
        i = int(k * sr)
        n = min(int(0.12 * sr), len(y) - i)
        tt = np.arange(n) / sr
        y[i:i + n] += 0.9 * np.sin(2 * np.pi * (50 + 80 * np.exp(-tt * 30)) * tt) * np.exp(-tt * 18)
    return y.astype(np.float32)


def acorde(frecuencias, dur_s=8, sr=SR):
    t = np.arange(int(sr * dur_s)) / sr
    return sum(0.2 * np.sin(2 * np.pi * f * t) for f in frecuencias).astype(np.float32)


# ── stems_worker ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("notas, camelot", [
    ((220.0, 261.63, 329.63), "8A"),   # La menor
    ((261.63, 329.63, 392.0), "8B"),   # Do mayor
    ((293.66, 369.99, 440.0), "10B"),  # Re mayor
])
def test_tonalidad_de(notas, camelot):
    clave, confianza = sw.tonalidad_de(acorde(notas), SR)
    assert clave == camelot
    assert 0 <= confianza <= 1


def test_tonalidad_de_silencio():
    assert sw.tonalidad_de(np.zeros(SR * 2, dtype=np.float32), SR) == (None, 0.0)


def test_tonalidad_con_afinacion_corrida():
    # La menor 40 cents arriba: con la afinación medida no cae en la nota vecina.
    corr = 2 ** (40 / 1200)
    clave, _ = sw.tonalidad_de(acorde([f * corr for f in (220.0, 261.63, 329.63)]), SR, cents=40)
    assert clave == "8A"


def test_fase_medida_ms_en_los_bombos():
    # 124 BPM, ancla 250 ms: el beat dura 483,9 ms y la fase se expresa en (−½ beat, ½ beat].
    fase = sw.fase_medida_ms(bombos(124, 0.25), SR, 124)
    assert fase is not None
    assert abs(fase - (250 - 60000 / 124)) <= 6


def test_fase_medida_ms_con_pocos_bombos():
    assert sw.fase_medida_ms(bombos(124, dur_s=4), SR, 124) is None


def test_ventanas_candidatas():
    # La pista suena de 1 a 24 y de 33 a 40: caben las ventanas que arrancan en 1, 9, 17 y 33.
    rangos = [{"from": 1, "to": 24}, {"from": 33, "to": 40}, {"from": None, "to": 5}]
    assert sw.ventanas_candidatas(rangos, 40) == [1, 9, 17, 33]
    assert sw.ventanas_candidatas([], 40) == []


def test_muestra_de_compas():
    # Compás 1 = el ancla; compás 2 = ancla + 4 beats.
    assert sw.muestra_de_compas(1, 120, 250, 1000) == 250
    assert sw.muestra_de_compas(2, 120, 250, 1000) == 2250


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="sin ffmpeg")
def test_lufs_of(tmp_path):
    import soundfile as sf

    p = tmp_path / "tono.wav"
    sf.write(p, (0.1 * np.sin(2 * np.pi * 1000 * np.arange(SR * 5) / SR)).astype(np.float32), SR)
    lufs = sw.lufs_of(p)
    assert lufs is not None and -26 < lufs < -18


def test_lufs_of_archivo_inexistente(tmp_path):
    assert sw.lufs_of(tmp_path / "no-existe.wav") is None


# ── grid_verifier ────────────────────────────────────────────────────────────
def test_jround_es_el_de_javascript():
    assert gv.jround(0.5) == 1 and gv.jround(1.5) == 2 and gv.jround(2.5) == 3
    assert gv.jround(-0.5) == 0


def test_eff_bpm():
    assert gv.eff_bpm({"bpm": 124, "bpm_fine": 0.25}) == pytest.approx(124.25)
    assert gv.eff_bpm({"bpm": 124, "bpm_detected": 124.3}) == 124       # a menos de 0,5: manda lo guardado
    assert gv.eff_bpm({"bpm": 124, "bpm_detected": 128}) == 128         # en conflicto: manda la detección
    assert gv.eff_bpm({"bpm_detected": "126.5"}) == 126.5
    assert gv.eff_bpm({"bpm": None, "bpm_detected": -1}) is None


def test_first_beat_ms_of():
    assert gv.first_beat_ms_of({"grid_source": "manual", "first_beat_offset_ms": 40,
                                "first_beat_detected_ms": 90}) == 40
    assert gv.first_beat_ms_of({"grid_source": "detectada", "first_beat_offset_ms": 40,
                                "first_beat_detected_ms": 90}) == 90
    assert gv.first_beat_ms_of({"first_beat_offset_ms": 40}) == 40
    assert gv.first_beat_ms_of({}) is None


def test_mide_el_decimal_y_corrige_el_ancla():
    # Tema real a 124,4 con catálogo 124 y ancla guardada 40 ms tarde.
    track = {"bpm": 124, "bpm_fine": 0, "first_beat_detected_ms": 290, "duration_seconds": 64}
    m = gv.measure_track(track, bombos(124.4, 0.25, dur_s=64), SR)
    assert m["bpmFine"] == pytest.approx(0.4, abs=0.02)
    assert abs(m["anchorMs"] - 250) <= 3
    assert m["passesGate"] is True
    assert gv.medida_invalida(gv.measurement_payload(m, None)) is None


def test_tema_corto_no_se_mide():
    track = {"bpm": 124, "first_beat_detected_ms": 250}
    with pytest.raises(RuntimeError, match="too_short"):
        gv.measure_track(track, bombos(124, dur_s=10), SR)
