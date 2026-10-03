"""W6 (3-oct-2026): casos borde de grid_detect.py que no tenían prueba.

Silencio, archivos cortísimos, semillas fuera de rango y el ancla que cae justo
después del primer beat. Ninguno debe colgar ni devolver un tempo imposible."""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import grid_detect as gd  # noqa: E402

SR = 11025


@pytest.mark.parametrize("cruda,esperada", [
    (30.0, 120.0),    # tres octavas abajo: ×4 da 120
    (20.0, 160.0),    # ×4 = 80 no alcanza; la corrección de siempre sube a 160
    (800.0, 100.0),   # muy arriba: se divide hasta entrar
    (1000.0, 125.0),
])
def test_semillas_muy_fuera_de_rango(cruda, esperada):
    octavas, otras = gd.semillas_candidatas(cruda)
    assert octavas and all(90 <= s <= 180 for s in octavas + otras)
    assert esperada in octavas


def test_semillas_sin_repetidas():
    octavas, otras = gd.semillas_candidatas(120.0)
    todas = octavas + otras
    assert octavas[0] == 120.0
    for i, a in enumerate(todas):
        for b in todas[i + 1:]:
            assert abs(a - b) / b > 0.03


@pytest.mark.parametrize("dur_s,periodo,fase", [
    (5.0, 0.5, 0.0),     # 10 beats: menos de 16
    (20.0, 0.5, 19.0),   # la fase deja menos de 16 beats
])
def test_grid_score_con_pocos_beats(dur_s, periodo, fase):
    env = np.ones(int(dur_s * SR / gd.HOP) + 1)
    assert gd._grid_score(env, SR, periodo, fase, dur_s) == -1e9


def test_grid_score_si_la_envolvente_es_mas_corta_que_el_audio():
    # Con 30 s de audio pero la envolvente de 5 s, quedan < 16 cuadros dentro.
    env = np.ones(int(5 * SR / gd.HOP))
    assert gd._grid_score(env, SR, 0.5, 0.0, 30.0) == -1e9


def test_busqueda_gruesa_no_sale_de_60_a_200(monkeypatch):
    vistos = []
    real = gd._grid_score

    def espia(env, sr, p, *resto):  # *resto: con #24 también recibe env3
        vistos.append(60.0 / p)
        return real(env, sr, p, *resto)

    monkeypatch.setattr(gd, "_grid_score", espia)
    bpm, _ = gd._busqueda_gruesa(np.ones(int(60 * SR / gd.HOP)), SR, 60.0, 196.0)
    assert vistos and max(vistos) <= 200 + 1e-6
    assert 60 <= bpm <= 200


def test_tempo_de_un_silencio_no_se_cuelga():
    """Con silencio librosa da 0: se usa 126 como semilla (30-sep, réplicas colgadas)."""
    y = np.zeros(SR * 20, dtype=np.float32)
    bpm, env = gd.detect_tempo(y, SR)
    assert 90 <= bpm <= 180
    assert np.all(env == 0)


def test_semilla_fuera_de_rango_se_ignora(monkeypatch):
    llamadas = []
    monkeypatch.setattr(gd.librosa.beat, "beat_track",
                        lambda **k: llamadas.append(1) or (np.array([124.0]), None))
    y = np.zeros(SR * 20, dtype=np.float32)
    gd.detect_tempo(y, SR, seed_bpm=250)
    assert llamadas == [1]  # 250 no sirve de semilla: se le pregunta a librosa


@pytest.mark.parametrize("muestras", [0, 100, int(SR * 0.04)])
def test_ancla_de_un_archivo_cortisimo(muestras):
    assert gd.detect_anchor(np.ones(muestras, dtype=np.float32), SR, 120) == 0


def test_ancla_de_un_silencio():
    assert gd.detect_anchor(np.zeros(SR * 2, dtype=np.float32), SR, 120) == 0


def bombo_en(segundos, dur_s=2.0):
    y = np.zeros(int(dur_s * SR))
    i = int(segundos * SR)
    n = int(0.1 * SR)
    tt = np.arange(n) / SR
    y[i:i + n] = 0.9 * np.sin(2 * np.pi * 55 * tt) * np.exp(-tt * 20)
    return y.astype(np.float32)


def test_ancla_en_el_primer_beat():
    assert abs(gd.detect_anchor(bombo_en(0.2), SR, 120) - 200) <= 10


def test_ancla_justo_despues_del_primer_beat_se_lleva_al_inicio():
    # A 120 BPM el beat dura 500 ms y la ventana llega a 525 ms. Un golpe en 510 ms
    # está en el beat 2: el ancla equivalente es 10 ms (REGLA 1).
    assert gd.detect_anchor(bombo_en(0.51), SR, 120) <= 20


def test_ancla_con_el_golpe_en_la_primera_muestra():
    y = bombo_en(0.0)
    assert gd.detect_anchor(y, SR, 120) <= 5
