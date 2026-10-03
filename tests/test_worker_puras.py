"""Funciones puras de worker.py sin pruebas (W6, noche 2→3-oct).

MIX-IN / MIX-OUT, control de calidad del análisis, energía por sección, ajuste de
fase de CM2 y piezas del render del set. Señales sintéticas; no hace falta ffmpeg.
"""
import os
import sys

import numpy as np
import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import worker  # noqa: E402

SR = 22050
SET_SR = worker.SET_SR


def kick(bpm, compases, sr=SR, amp=0.9):
    """Bombo de 60 Hz en cada negra durante `compases` compases de 4/4."""
    beat = int(60.0 / bpm * sr)
    golpe = (amp * np.sin(2 * np.pi * 60 * np.arange(int(0.15 * sr)) / sr)
             * np.exp(-np.arange(int(0.15 * sr)) / (0.05 * sr))).astype(np.float32)
    y = np.zeros(beat * 4 * compases, dtype=np.float32)
    for i in range(0, len(y) - len(golpe), beat):
        y[i:i + len(golpe)] += golpe
    return y


def ruido(segundos, amp, sr=SR, semilla=0):
    return (np.random.RandomState(semilla).randn(int(segundos * sr)) * amp).astype(np.float32)


def cues(**pos):
    nombres = {"mix_in": "MIX-IN", "mix_out": "MIX-OUT", "drop": "DROP 1"}
    return [{"label": nombres[k], "positionMs": v} for k, v in pos.items()]


# ───────────────────────────── MIX-IN / MIX-OUT ─────────────────────────────
def test_mix_in_en_la_primera_frase_con_bombo():
    # 16 compases de intro sin bombo (solo agudos) y 48 con bombo, a 120 BPM (compás = 2 s)
    intro = ruido(32, 0.05)
    intro -= np.convolve(intro, np.ones(64) / 64, mode="same")   # sin graves
    y = np.concatenate([intro, kick(120, 48)])
    pos, dj = worker.detect_mix_in(y, SR, 120, 0.0)
    assert pos == pytest.approx(32000, abs=1)
    assert dj is True


def test_mix_in_sin_intro_no_es_dj_friendly():
    pos, dj = worker.detect_mix_in(kick(120, 40), SR, 120, 0.0)
    assert pos == 0 and dj is False


def test_mix_in_tema_muy_corto():
    assert worker.detect_mix_in(kick(120, 6), SR, 120, 0.0) == (None, False)


def test_mix_out_elige_la_caida_sostenida_del_final():
    # 80 compases: breakdown temprano (compases 20–27) que NO es el final, outro desde el 60
    partes = [ruido(40, 0.3), ruido(16, 0.05, semilla=1), ruido(64, 0.3, semilla=2), ruido(40, 0.06, semilla=3)]
    pos, ok = worker.detect_mix_out(np.concatenate(partes), SR, 120, 0.0)
    assert ok is True
    assert pos == 120000                     # compás 60 × 2 s


def test_mix_out_sin_caida_usa_el_respaldo_con_pista_de_salida():
    pos, ok = worker.detect_mix_out(ruido(160, 0.3), SR, 120, 0.0)
    assert ok is False
    # 87 % de 80 compases = 70, recortado para dejar 16 compases de salida → 64
    assert pos == 64 * 2000


def test_mix_out_tema_corto():
    assert worker.detect_mix_out(ruido(20, 0.3), SR, 120, 0.0) == (None, False)


# ───────────────────────────── energía por sección ─────────────────────────────
def test_energia_por_seccion():
    y = np.concatenate([ruido(30, 0.05), ruido(30, 0.1, semilla=1), ruido(60, 0.4, semilla=2), ruido(60, 0.1, semilla=3)])
    e = worker.compute_section_energy(y, SR, cues(mix_in=0, drop=60000, mix_out=180000), 180000)
    assert e["peak"] == 9
    assert e["entry"] < e["exit"] < e["peak"]


def test_energia_por_seccion_sin_cues_no_falla():
    e = worker.compute_section_energy(ruido(60, 0.2), SR, None, 60000)
    assert set(e) == {"entry", "peak", "exit"}
    assert all(1 <= v <= 9 for v in e.values())


# ───────────────────────────── control de calidad ─────────────────────────────
def test_sanity_check_sin_problemas():
    r = {"bpm": 124, "cue_points": cues(mix_in=10000, mix_out=300000)}
    assert worker.sanity_check(r, 360000) == ([], 1.0)


def test_sanity_check_cuenta_cada_problema():
    r = {"bpm": 124, "bpm_precise": 164.2, "tempo_stability": "variable",
         "cue_points": cues(mix_in=100000, mix_out=90000)}
    problemas, conf = worker.sanity_check(r, 200000)
    nombres = {p.split(":")[0] for p in problemas}
    assert nombres == {"bpm_fuera_de_rango", "mixin_tarde", "mixout_temprano", "orden_invertido", "tempo_variable"}
    assert conf == 0.0                       # nunca negativa
    # el BPM que se mira es el refinado, no el de detect_grid
    assert "bpm_fuera_de_rango:164.2" in problemas


def test_sanity_check_pista_de_salida_corta():
    r = {"bpm": 124, "cue_points": cues(mix_in=0, mix_out=190000)}
    problemas, conf = worker.sanity_check(r, 200000)
    assert problemas == ["runway_corto:10s"] and conf == 0.8


# ───────────────────────────── fase de CM2 ─────────────────────────────
def test_fase_por_segmento_mide_el_desfase_del_peine():
    periodo, desfase = 0.5, 0.125            # un cuarto de pulso → π/2
    hop = 512 / SR
    times = np.arange(int(60 / hop)) * hop
    onset = np.zeros_like(times)
    for t in np.arange(desfase, 60, periodo):
        onset[int(round(t / hop))] = 1.0
    pts = worker._fase_por_segmento(onset, times, periodo)
    assert len(pts) == 12
    for _, ang, peso in pts:
        assert ang == pytest.approx(np.pi / 2, abs=0.1)
        assert peso > 0


def test_fase_por_segmento_salta_los_tramos_mudos():
    times = np.arange(1200) * 0.01
    onset = np.zeros(1200)
    onset[[0, 50]] = 1.0                      # solo el primer segmento tiene golpes
    assert len(worker._fase_por_segmento(onset, times, 0.5)) == 1
    parejo = np.zeros(1200)
    parejo[:100] = 1.0                        # energía pareja, sin pulso: no hay fase que medir
    assert worker._fase_por_segmento(parejo, times, 0.5) == []


def test_ajuste_lineal_fase_recupera_el_tempo_fino():
    periodo = 60.0 / 124                      # nominal 124 BPM
    real_hz = 124.05 / 60.0                   # suena a 124,05
    b = 0.7
    T = np.linspace(5, 300, 12)
    a = 2 * np.pi * (real_hz - 1 / periodo)
    fase = np.angle(np.exp(1j * (a * T + b)))  # la fase medida viene envuelta en (−π, π]
    hz, b_est, resid_ms = worker._ajuste_lineal_fase([(t, f, 1.0) for t, f in zip(T, fase)], periodo)
    assert hz * 60 == pytest.approx(124.05, abs=1e-6)
    assert b_est == pytest.approx(b, abs=1e-6)
    assert resid_ms < 1e-6


def test_ajuste_lineal_fase_pide_tres_segmentos():
    with pytest.raises(RuntimeError, match="CM2"):
        worker._ajuste_lineal_fase([(1.0, 0.0, 1.0), (2.0, 0.1, 1.0)], 0.5)


# ───────────────────────────── piezas del render del set ─────────────────────────────
def amplitud(x, hz, sr=SET_SR):
    t = np.arange(len(x)) / sr
    return float(np.abs(np.mean(x * np.exp(-2j * np.pi * hz * t))) * 2)


def estereo(*tonos, seg=1.0):
    t = np.arange(int(seg * SET_SR)) / SET_SR
    m = sum(a * np.sin(2 * np.pi * hz * t) for hz, a in tonos).astype(np.float32)
    return np.stack([m, m], axis=1)


def test_low_shelf_recorta_solo_los_graves():
    x = estereo((30, 0.4), (5000, 0.4))
    y = worker._low_shelf(x, worker.BASS_HZ, -24.0)
    assert y.shape == x.shape
    medio = slice(SET_SR // 4, -SET_SR // 4)
    assert 20 * np.log10(amplitud(y[medio, 0], 30) / 0.4) < -15
    assert amplitud(y[medio, 0], 5000) == pytest.approx(0.4, rel=0.05)


def test_graves_por_muestra():
    x = estereo((40, 0.4), (3000, 0.4))
    assert worker._graves(x, np.zeros(len(x))) is x
    y = worker._graves(x, np.full(len(x), -40.0))
    medio = slice(SET_SR // 4, -SET_SR // 4)
    assert amplitud(y[medio, 0], 40) < 0.05
    assert amplitud(y[medio, 0], 3000) == pytest.approx(0.4, rel=0.05)


def test_unir_cruza_10_ms_sin_perder_ni_sumar_de_mas():
    a = np.ones((1000, 2), dtype=np.float32)
    b = np.zeros((1000, 2), dtype=np.float32)
    n = int(0.01 * SET_SR)
    u = worker._unir(a, b)
    assert len(u) == 2000 - n
    assert u[0, 0] == 1.0 and u[-1, 0] == 0.0
    assert np.all(np.diff(u[:, 0]) <= 1e-6)   # baja sin saltos
    assert len(worker._unir(np.zeros((0, 2)), b)) == 1000


def test_fin_util_quita_la_cola_de_silencio():
    x = np.zeros((1000, 2), dtype=np.float32)
    x[:600] = 0.5
    x[600:] = 1e-5                            # −100 dB: cuenta como silencio
    assert worker.fin_util(x) == 600
    assert worker.fin_util(np.zeros((50, 2))) == 50
    assert worker.fin_util(np.zeros((0, 2))) == 0


def test_cola_eco_a_tempo():
    seca = np.zeros((100, 2), dtype=np.float32)
    seca[0] = 1.0
    cola = worker.cola_eco(seca, 120)
    d = int(0.5 * SET_SR)
    assert len(cola) == int(0.5 * 8 * SET_SR) + 100
    assert cola[0, 0] == 0.0                  # lo seco no va en la cola
    assert cola[d, 0] == pytest.approx(worker.ECO_WET)
    assert cola[2 * d, 0] == pytest.approx(worker.ECO_WET * worker.ECO_FEEDBACK)
    assert cola[-1, 0] == 0.0                 # el retorno termina en 0
    assert len(worker.cola_eco(seca, 0)) == int(0.5 * 8 * SET_SR) + 100   # sin BPM: 120 por defecto


def test_cue_sin_importar_mayusculas():
    c = [{"label": "mix-in", "positionMs": 1234}, {"label": None, "positionMs": 1}]
    assert worker._cue(c, "MIX-IN") == 1234.0
    assert worker._cue(c, "DROP 1", 99) == 99
    assert worker._cue(None, "MIX-IN") is None


def estirar_falso(x, tempo, _tmp):
    """Reemplazo de ffmpeg: re-muestrea (tempo > 1 acorta), sin conservar el tono."""
    if abs(tempo - 1.0) < 1e-4 or len(x) == 0:
        return x
    n = int(round(len(x) / tempo))
    src = np.linspace(0, len(x) - 1, n)
    return np.stack([np.interp(src, np.arange(len(x)), x[:, c]) for c in range(2)], axis=1).astype(np.float32)


def test_linea_entrante_sin_ajuste_es_el_audio_desde_la_entrada(tmp_path):
    audio = np.random.RandomState(0).randn(SET_SR * 10, 2).astype(np.float32)
    pcm, tramos = worker.linea_entrante(audio, 2.0, 8.0, 1.0, 4.0, str(tmp_path))
    assert np.array_equal(pcm, audio[2 * SET_SR:])
    assert tramos == [(2.0, 0.0, 1.0)]


def test_linea_entrante_estira_y_ubica_los_puntos(monkeypatch, tmp_path):
    monkeypatch.setattr(worker, "_estirar_pcm", estirar_falso)
    audio = np.random.RandomState(0).randn(SET_SR * 60, 2).astype(np.float32)
    rate, dur, rel = 1.02, 16.0, 8.0
    pcm, tramos = worker.linea_entrante(audio, 4.0, dur, rate, rel, str(tmp_path))
    medio = (1 + rate) / 2
    assert tramos[0] == (4.0, 0.0, rate)
    assert tramos[1][1] == dur and tramos[1][2] == medio
    assert tramos[2][1] == dur + rel and tramos[2][2] == 1.0
    # el tramo estirado dura `dur` en la línea y la rampa, `rel`
    n = int(0.01 * SET_SR)
    resto = len(audio) - int(4.0 * SET_SR) - int(dur * rate * SET_SR) - int(rel * medio * SET_SR)
    assert len(pcm) == pytest.approx(int(dur * SET_SR) + int(rel * SET_SR) + resto - 2 * n, abs=3)
    # un punto del tema después de la rampa cae donde dice seg_en_linea
    assert worker.seg_en_linea(tramos, tramos[2][0] + 5) == pytest.approx(dur + rel + 5)


def test_linea_entrante_sin_estirar_avisa(monkeypatch, tmp_path):
    monkeypatch.setattr(worker, "_estirar_pcm", lambda *a: None)
    audio = np.zeros((SET_SR * 30, 2), dtype=np.float32)
    with pytest.raises(worker.SinEstirar):
        worker.linea_entrante(audio, 0.0, 8.0, 0.98, 4.0, str(tmp_path))
