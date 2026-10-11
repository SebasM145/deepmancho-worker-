"""Noche del 11-oct-2026: las funciones de cálculo del verificador de rejilla que no tenían prueba.

Son el port de `gridVerify.ts`/`lowEnvelope.ts` del mixer. Si alguien las toca, el verificador
deja de medir igual que el navegador y empieza a mover anclas que el DJ ve bien. Todo con
números o audio sintético, sin red ni ffmpeg.
"""
import math
import os
import sys

import numpy as np
import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import grid_verifier as gv  # noqa: E402

BIN_RATE = 500.0
BINS = gv.FOLD_BINS


def tren(bpm, fase=0.0, segundos=40, peso=1.0):
    """Ataques de onset (un bin) en cada beat de `bpm`, corridos `fase` de beat."""
    osf = np.zeros(int(segundos * BIN_RATE), dtype=np.float32)
    periodo = 60 / bpm * BIN_RATE
    i = fase * periodo
    while i < osf.shape[0]:
        osf[int(i)] += peso
        i += periodo
    return osf


# ── r3 y fold01 ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("x, esperado", [(124.0004, 124.0), (124.0005, 124.001), (-0.0005, 0.0), (0.12345, 0.123)])
def test_r3_redondea_a_milesimas_como_javascript(x, esperado):
    assert gv.r3(x) == pytest.approx(esperado, abs=1e-9)


@pytest.mark.parametrize("x, esperado", [(0.2, 0.2), (0.7, -0.3), (1.25, 0.25), (-0.2, -0.2), (0.5, -0.5), (3.0, 0.0)])
def test_fold01_lleva_la_fase_a_medio_beat_de_cada_lado(x, esperado):
    assert gv.fold01(x) == pytest.approx(esperado)
    assert -0.5 <= gv.fold01(x) < 0.5


# ── fold_histogram ───────────────────────────────────────────────────────────
def test_fold_histogram_junta_cada_beat_en_su_bin_de_fase():
    periodo = 250.0  # 120 BPM a 500 bins/s
    osf = np.zeros(int(periodo * 32), dtype=np.float32)
    osf[125::250] = 1.0  # medio beat
    H = gv.fold_histogram(osf, periodo)
    assert H.shape == (BINS,)
    assert int(np.argmax(H)) == BINS // 2
    assert H.sum() == pytest.approx(32)
    assert np.count_nonzero(H) == 1


def test_fold_histogram_ignora_los_valores_negativos_y_ceros():
    osf = np.array([0.0, -5.0, 2.0, 0.0, -1.0, 3.0], dtype=np.float32)
    H = gv.fold_histogram(osf, 3.0)
    assert H.sum() == pytest.approx(5.0)
    assert (H >= 0).all()


# ── fold_peak ────────────────────────────────────────────────────────────────
def test_fold_peak_de_un_solo_bin():
    H = np.zeros(BINS)
    H[30] = 10.0
    pk = gv.fold_peak(H)
    assert pk["peakBin"] == 30
    assert pk["phase"] == pytest.approx((30 + 0.5) / BINS)
    # S[30] = 5 y la media de S es 10/96: nitidez 48. Sin un segundo pico lejos, peakRatio tope.
    assert pk["sharp"] == pytest.approx(48.0)
    assert pk["peakRatio"] == 99.0


def test_fold_peak_plano_no_tiene_pico_claro():
    pk = gv.fold_peak(np.ones(BINS))
    assert pk["sharp"] == pytest.approx(1.0)
    assert pk["peakRatio"] == pytest.approx(1.0)


def test_fold_peak_sin_energia_no_divide_por_cero():
    pk = gv.fold_peak(np.zeros(BINS))
    assert pk["sharp"] == 0.0
    assert pk["peakRatio"] == 99.0
    assert pk["phase"] == pytest.approx(0.5 / BINS)


def test_fold_peak_segundo_pico_lejano_marca_la_ambiguedad():
    H = np.zeros(BINS)
    H[10] = 8.0
    H[60] = 4.0  # a 50 bins: cuenta como rival
    pk = gv.fold_peak(H)
    assert pk["peakBin"] == 10
    assert pk["peakRatio"] == pytest.approx(2.0)


def test_fold_peak_vecino_cercano_no_cuenta_como_rival():
    H = np.zeros(BINS)
    H[10] = 8.0
    H[15] = 7.0  # a 5 bins (≤ 8): es el mismo golpe ensanchado, no otro pico
    assert gv.fold_peak(H)["peakRatio"] == 99.0


def test_fold_peak_fase_cruza_el_cero_sin_saltar():
    # Lóbulo repartido entre el último y el primer bin: la fase circular queda en ~0, no en 0,5.
    H = np.zeros(BINS)
    H[BINS - 1] = 5.0
    H[0] = 5.0
    ph = gv.fold_peak(H)["phase"]
    assert min(ph, 1 - ph) < 1.0 / BINS


# ── tempo_sharpness / scan_tempo ─────────────────────────────────────────────
def test_tempo_sharpness_es_maxima_en_el_tempo_real():
    osf = tren(124.0)
    en_124 = gv.tempo_sharpness(osf, BIN_RATE, 124.0)
    assert en_124 > 10
    for otro in (122.0, 123.0, 125.0, 126.0):
        assert gv.tempo_sharpness(osf, BIN_RATE, otro) < en_124 / 2


def test_scan_tempo_encuentra_el_decimal():
    r = gv.scan_tempo(tren(124.4), BIN_RATE, [124.0])
    assert r["bpm"] == pytest.approx(124.4, abs=0.01)


def test_scan_tempo_se_queda_en_el_candidato_mas_nitido():
    r = gv.scan_tempo(tren(128.0), BIN_RATE, [124.0, 128.0])
    assert r["bpm"] == pytest.approx(128.0, abs=0.01)


# ── onset_raw / onset_attack ─────────────────────────────────────────────────
def test_onset_raw_solo_las_subidas():
    env = np.array([0.0, 1.0, 0.5, 0.5, 2.0], dtype=np.float32)
    assert gv.onset_raw(env).tolist() == pytest.approx([0.0, 1.0, 0.0, 0.0, 1.5])


def test_onset_attack_sube_en_log_y_no_es_negativo():
    env = np.concatenate([np.full(10, 0.01), np.full(10, 1.0), np.full(10, 0.01)]).astype(np.float32)
    a = gv.onset_attack(env, k=3)
    assert a.shape == env.shape
    assert (a >= 0).all()
    assert a[:3].tolist() == [0.0, 0.0, 0.0]
    assert int(np.argmax(a)) in range(10, 13)
    assert a[20:].max() == 0.0  # la bajada no cuenta


@pytest.mark.parametrize("n", [0, 2, 3])
def test_onset_attack_con_envolvente_mas_corta_que_k(n):
    a = gv.onset_attack(np.ones(n, dtype=np.float32), k=3)
    assert a.shape == (n,)
    assert not a.any()


# ── low_envelope ─────────────────────────────────────────────────────────────
def test_low_envelope_bin_rate_exacta_y_deja_pasar_graves():
    sr = 44100
    t = np.arange(sr * 2) / sr
    grave = np.sin(2 * np.pi * 60 * t).astype(np.float32)
    agudo = np.sin(2 * np.pi * 5000 * t).astype(np.float32)
    env_g, rate = gv.low_envelope(grave, sr)
    env_a, _ = gv.low_envelope(agudo, sr)
    assert rate == pytest.approx(sr / round(sr / gv.NOMINAL_HZ))
    assert env_g.shape[0] == (sr * 2) // round(sr / gv.NOMINAL_HZ)
    assert env_g.dtype == np.float32
    assert env_g[100:].mean() > 10 * env_a[100:].mean()


# ── bpm_fine_for / stored_bpm / _fpos ────────────────────────────────────────
@pytest.mark.parametrize("entero, medido, fino", [(128, 128.0704, 0.07), (128, 127.9, -0.1), (124, 124.0, 0.0)])
def test_bpm_fine_for(entero, medido, fino):
    assert gv.bpm_fine_for(entero, medido) == pytest.approx(fino, abs=1e-9)


@pytest.mark.parametrize("track, esperado", [
    ({"bpm": 128, "bpm_fine": 0.07}, 128.07),          # tempo = bpm + bpm_fine
    ({"bpm": 128}, 128.0),
    ({"bpm": 128, "bpm_fine": None}, 128.0),
    ({"bpm": "126", "bpm_fine": "-0.5"}, 125.5),
    ({"bpm": 128, "bpm_fine": float("nan")}, 128.0),   # decimal roto: se ignora, no tumba el tempo
    ({"bpm": None, "bpm_fine": 0.3}, None),
    ({}, None),
    ({"bpm": 0}, None),
    ({"bpm": 0.2, "bpm_fine": -0.5}, None),
    ({"bpm": float("inf")}, None),
])
def test_stored_bpm(track, esperado):
    r = gv.stored_bpm(track)
    if esperado is None:
        assert r is None
    else:
        assert r == pytest.approx(esperado)


@pytest.mark.parametrize("v, esperado", [
    (128, 128.0), ("124.5", 124.5), (None, None), ("abc", None), (0, None), (-3, None),
    (float("inf"), None), (float("nan"), None), ([], None),
])
def test_fpos(v, esperado):
    assert gv._fpos(v) == esperado


# ── assemble_result / _keep ──────────────────────────────────────────────────
def _fase(ok=True, anchor=0.25, delta=0.012, sharp=3.0, pr=1.876, reason="near_prior"):
    return {"anchorSec": anchor, "deltaSec": delta, "sharp": sharp, "peakRatio": pr, "ok": ok, "reason": reason}


def test_assemble_result_campos_y_redondeos():
    env = np.ones(int(40 * BIN_RATE), dtype=np.float32)
    r = gv.assemble_result(env, BIN_RATE, 0.238, 124.0, {"bpm": 124.4004, "sharp": 2.5}, _fase())
    assert r["anchorMs"] == 250
    assert r["bpm"] == 124.4
    assert r["deltaAnchorMs"] == 12.0
    assert r["deltaBpm"] == pytest.approx(0.4)
    assert r["conf"] == 1.0                 # (2,5 − 1) / 1,5
    assert r["peakRatio"] == 1.88
    assert r["windowsGood"] == r["windowsTotal"] == 5
    assert r["residualBeats"] == 0
    assert r["tempoSharp"] == 2.5 and r["phaseSharp"] == 3.0
    assert r["phaseOk"] is True and r["phaseReason"] == "near_prior"
    assert r["candidateBpm"] == 124.0


@pytest.mark.parametrize("sharp, conf", [(0.5, 0.0), (1.0, 0.0), (1.75, 0.5), (9.0, 1.0)])
def test_assemble_result_confianza_entre_0_y_1(sharp, conf):
    env = np.ones(int(40 * BIN_RATE), dtype=np.float32)
    r = gv.assemble_result(env, BIN_RATE, 0.0, 124.0, {"bpm": 124.0, "sharp": sharp}, _fase())
    assert r["conf"] == pytest.approx(conf)


def test_assemble_result_ancla_negativa_queda_en_cero():
    env = np.ones(int(40 * BIN_RATE), dtype=np.float32)
    r = gv.assemble_result(env, BIN_RATE, 0.0, 124.0, {"bpm": 124.0, "sharp": 2.0}, _fase(anchor=-0.03, delta=-0.03))
    assert r["anchorMs"] == 0
    assert r["deltaAnchorMs"] == -30.0


def test_assemble_result_fase_no_confirmada_no_frena_la_puerta_por_peak_ratio():
    """Con la fase sin confirmar se conserva el ancla y peakRatio va en 9,9 (como gridVerify.ts)."""
    env = np.ones(int(40 * BIN_RATE), dtype=np.float32)
    fase = gv._keep(0.25, "phase_flat", 1.1, 1.05)
    assert fase == {"anchorSec": 0.25, "deltaSec": 0.0, "sharp": 1.1, "peakRatio": 1.05, "ok": False, "reason": "phase_flat"}
    r = gv.assemble_result(env, BIN_RATE, 0.25, 124.0, {"bpm": 124.0, "sharp": 2.0}, fase)
    assert r["peakRatio"] == 9.9
    assert r["anchorMs"] == 250 and r["deltaAnchorMs"] == 0.0
    assert r["phaseOk"] is False


def test_resultado_completo_pasa_la_puerta_y_el_plano_no():
    env_bueno = np.zeros(int(64 * BIN_RATE), dtype=np.float32)
    env_bueno[::int(60 / 124 * BIN_RATE)] = 1.0
    r = gv.verify_grid_whole(env_bueno + 0.01, BIN_RATE, 0.0, [124.0])
    assert r is not None and gv.passes_grid_gate(r)
    plano = gv.verify_grid_whole(np.full(int(64 * BIN_RATE), 0.5, dtype=np.float32), BIN_RATE, 0.0, [124.0])
    assert plano is not None and not gv.passes_grid_gate(plano)
    assert math.isfinite(plano["conf"]) and plano["conf"] == 0.0
