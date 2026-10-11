"""Medidas del banco A/B de separación (banco_separacion.py), noche del 11-oct.

El banco decide entre demucs y el híbrido con estos números: si una medida se
tuerce, el banco recomienda el motor equivocado sin que nadie lo note. Aquí se
fijan con señales sintéticas (numpy), sin demucs, sin red y sin ffmpeg.
"""
import os
import random
import sys

import numpy as np
import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import banco_separacion as bs  # noqa: E402

SR = bs.SR


def tono(hz, dur_s, sr=SR, amp=0.5):
    t = np.arange(int(dur_s * sr)) / sr
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float64)


def ruido(dur_s, semilla=1, sr=SR):
    return np.random.default_rng(semilla).standard_normal(int(dur_s * sr))


def bombos_en(tiempos_s, dur_s, sr=SR):
    """Un golpe grave (60 Hz que decae) en cada instante de `tiempos_s`."""
    y = np.zeros(int(dur_s * sr))
    n = int(0.12 * sr)
    golpe = tono(60, 0.12, sr, 0.9) * np.exp(-np.arange(n) / (0.03 * sr))
    for t in tiempos_s:
        i = int(round(t * sr))
        if i + n <= len(y):
            y[i:i + n] += golpe
    return y


# ───────────────────────────── filtro de banda y envolvente ─────────────────────────────
def test_banda_deja_lo_de_adentro_y_quita_lo_de_afuera():
    dentro, fuera = tono(2000, 1.0), tono(200, 1.0)
    y = bs._banda(dentro + fuera, SR, 1000, 4000)
    assert len(y) == len(dentro)
    assert np.corrcoef(y, dentro)[0, 1] > 0.999
    assert abs(np.dot(y, fuera)) / np.dot(fuera, fuera) < 1e-3


def test_env_es_el_rms_por_ventana():
    y = np.full(SR, 0.5)                      # constante: RMS = 0,5 en todas las ventanas
    e = bs._env(y, SR, ms=20)
    assert len(e) == SR // int(SR * 0.02)
    assert np.allclose(e, 0.5, atol=1e-6)


def test_env_de_audio_mas_corto_que_una_ventana_no_revienta():
    e = bs._env(np.ones(10), SR, ms=20)
    assert e.shape == (1,) and e[0] == 0.0


# ───────────────────────────── correlación ─────────────────────────────
def test_corr_igual_opuesta_y_sin_relacion():
    a = ruido(1.0, 1)
    assert bs._corr(a, a) == pytest.approx(1.0)
    assert bs._corr(a, -a) == pytest.approx(-1.0)
    assert abs(bs._corr(a, ruido(1.0, 2))) < 0.05


def test_corr_usa_el_largo_comun():
    a = ruido(1.0, 3)
    assert bs._corr(a, a[: len(a) // 2]) == pytest.approx(1.0)


def test_corr_con_muy_pocas_muestras_o_constante_da_cero_y_no_nan():
    assert bs._corr(np.arange(7.0), np.arange(7.0)) == 0.0
    r = bs._corr(np.ones(100), ruido(0.01))
    assert r == pytest.approx(0.0) and not np.isnan(r)


# ───────────────────────────── alineación ─────────────────────────────
@pytest.mark.parametrize("lag", [0, 300, -300, 573])
def test_alinear_encuentra_el_desfase_con_signo(lag):
    """+ = la pista viene tarde (el caso de los mp3 de demucs, ~26 ms)."""
    ref = ruido(3.0, 4)
    y = bs._desplazar(ref, -lag)               # correrla `lag` muestras hacia adelante
    assert bs._alinear(ref, y, SR) == lag


def test_alinear_no_busca_mas_alla_de_max_ms():
    ref = ruido(3.0, 5)
    k = int(SR * 0.060)
    lag = bs._alinear(ref, bs._desplazar(ref, -(k + 500)), SR, max_ms=60)
    assert abs(lag) <= k


def test_desplazar_recorta_o_rellena_con_ceros():
    y = np.arange(1, 6, dtype=np.float32)
    assert list(bs._desplazar(y, 2)) == [3, 4, 5]
    rell = bs._desplazar(y, -2)
    assert list(rell) == [0, 0, 1, 2, 3, 4, 5] and rell.dtype == np.float32
    assert bs._desplazar(y, 0) is y


# ───────────────────────────── fuga de voz ─────────────────────────────
def test_fuga_voz_db_mide_cuanta_voz_queda_en_el_instrumental():
    voz = ruido(4.0, 6)
    retardo = int(SR * 0.026)                  # 26 ms tarde, como un mp3 sin cabecera
    inst = bs._desplazar(0.1 * voz, -retardo) + 0.5 * ruido(4.0 + retardo / SR, 7)[: len(voz) + retardo]
    db, lag_ms = bs.fuga_voz_db(inst, voz, SR)
    assert db == pytest.approx(-20.0, abs=1.0)    # 10 % de la voz = −20 dB
    assert lag_ms == pytest.approx(26.0, abs=0.1)


def test_fuga_voz_db_sin_voz_es_muy_negativa():
    db, _ = bs.fuga_voz_db(ruido(4.0, 8), ruido(4.0, 9), SR)
    assert db < -30


def test_fuga_voz_db_sin_voz_de_referencia_no_da_nan():
    db, _ = bs.fuga_voz_db(ruido(2.0, 10), np.zeros(int(2.0 * SR)), SR)
    assert np.isfinite(db)


# ───────────────────────────── bombo contra la rejilla ─────────────────────────────
def test_desvio_bombo_en_la_rejilla_corrida_10ms():
    bpm, beat = 124.0, 60.0 / 124
    tiempos = [0.5 + 0.010 + i * beat for i in range(40)]
    y = bombos_en(tiempos, 22.0)
    med_abs, med, deriva, n = bs.desvio_bombo(y, SR, bpm, ancla_ms=500.0)
    assert n == 40
    assert med == pytest.approx(med_abs, abs=0.5)      # todos tarde: el signo es +
    assert 5.0 < med < 15.0                            # ~10 ms más el arranque del golpe
    assert abs(deriva) < 1.0


def test_desvio_bombo_temprano_tiene_signo_negativo():
    bpm, beat = 124.0, 60.0 / 124
    tiempos = [0.5 - 0.030 + i * beat for i in range(40)]
    _, med, _, _ = bs.desvio_bombo(bombos_en(tiempos, 22.0), SR, bpm, ancla_ms=500.0)
    assert med < -15.0


def test_desvio_bombo_detecta_la_deriva():
    """Bombos a 124,5 contra una rejilla de 124: se van atrasando cada compás."""
    tiempos = [0.5 + i * 60.0 / 124.5 for i in range(40)]
    _, _, deriva, _ = bs.desvio_bombo(bombos_en(tiempos, 22.0), SR, 124.0, ancla_ms=500.0)
    esperado = (60 / 124.5 - 60 / 124) / (60 / 124.5) * 1000 * 8 * 240 / 124   # ms cada 8 compases
    assert deriva == pytest.approx(esperado, rel=0.1)
    assert deriva < 0


def test_desvio_bombo_con_menos_de_4_golpes_no_inventa():
    y = bombos_en([0.5, 1.0, 1.5], 3.0)
    assert bs.desvio_bombo(y, SR, 120.0, 0.0) == (None, None, None, 3)


# ───────────────────────────── F1 de notas del bajo ─────────────────────────────
def n(nota, beat):
    return {"n": nota, "b": beat}


def test_f1_sin_notas_en_ninguno_es_vacio():
    assert bs.f1_notas([], []) is None


def test_f1_perfecto_y_dentro_de_la_tolerancia():
    ref = [n(36, 0), n(38, 1), n(40, 2)]
    assert bs.f1_notas(ref, ref) == 1.0
    assert bs.f1_notas([n(36, 0.2), n(38, 1.25), n(40, 1.8)], ref) == 1.0


def test_f1_fuera_de_tolerancia_o_altura_distinta_no_cuenta():
    ref = [n(36, 0), n(38, 1)]
    assert bs.f1_notas([n(36, 0.3), n(37, 1)], ref) == 0.0


def test_f1_un_lado_vacio_es_cero():
    assert bs.f1_notas([], [n(36, 0)]) == 0.0
    assert bs.f1_notas([n(36, 0)], []) == 0.0


def test_f1_una_nota_de_referencia_no_se_cuenta_dos_veces():
    """Dos notas estimadas sobre la misma referencia: precisión 1/2, recall 1 → F1 0,667."""
    assert bs.f1_notas([n(36, 0), n(36, 0.1)], [n(36, 0)]) == pytest.approx(0.667)


# ───────────────────────────── tempo, resumen y escucha ciega ─────────────────────────────
def test_tempo_y_ancla_usa_tempos_csv_sin_abrir_el_audio(tmp_path):
    src = tmp_path / "no-existe.mp3"
    assert bs.tempo_y_ancla(src, {"no-existe.mp3": (126.0, 42.0)}) == (126.0, 42.0, "tempos.csv")


def fila(motor, tema, **kw):
    return {"motor": motor, "tema": tema, "engine_version": f"{motor}-v1", **kw}


def test_resumen_promedia_por_motor_e_ignora_lo_vacio():
    filas = [
        fila("demucs", "a", segundos=10, fuga_voz_db="", calidad_vocals="limpia"),
        fila("demucs", "b", segundos=20, fuga_voz_db=None, calidad_vocals="con_algo"),
        fila("hibrido", "a", segundos=30, fuga_voz_db=-25.0, calidad_vocals="limpia"),
    ]
    txt = bs.resumen(filas)
    assert "temas: 2" in txt
    bloque_demucs = txt.split("[demucs]")[1].split("[hibrido]")[0]
    assert "segundos" in bloque_demucs and "media     15.00" in bloque_demucs
    assert "fuga_voz_db" not in bloque_demucs.split("fuga_voz_corr es")[0]
    assert "{'con_algo': 1, 'limpia': 1}" in bloque_demucs
    assert "fuga_voz_db" in txt.split("[hibrido]")[1]


def test_escucha_ciega_reparte_letras_y_se_repite_con_la_misma_semilla(tmp_path, monkeypatch):
    llamadas = []
    monkeypatch.setattr(bs.subprocess, "run", lambda cmd, check: llamadas.append(cmd))
    runs = {m: {"pistas": {"vocals": tmp_path / f"{m}-v.mp3", "drums": tmp_path / f"{m}-d.mp3"}}
            for m in ("demucs", "hibrido")}
    clave1, clave2 = {}, {}
    bs.escucha_ciega(tmp_path, "tema", runs, clave1, random.Random(7))
    bs.escucha_ciega(tmp_path, "tema", runs, clave2, random.Random(7))
    assert clave1 == clave2
    assert set(clave1["tema"]) == {"A", "B"}
    assert sorted(clave1["tema"].values()) == ["demucs", "hibrido"]
    assert (tmp_path / "escucha_ciega" / "tema" / "A").is_dir()
    destinos = {c[-1] for c in llamadas}
    assert any(d.endswith("escucha_ciega/tema/A/vocals.wav") for d in destinos)
    assert len(llamadas) == 8                  # 2 motores × 2 pistas × 2 corridas


def test_main_rechaza_un_motor_desconocido(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["banco", str(tmp_path / "x.mp3"), "--motores", "demucs,otro"])
    with pytest.raises(SystemExit):
        bs.main()


def test_main_sin_audios_avisa(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["banco"])
    with pytest.raises(SystemExit):
        bs.main()
