"""#618 (4-oct): los temas del Taller (100 BPM exactos de rejilla de máquina, Entrada de
19,2 s sin bombo, voz) salían «tempo variable» (residuo 136–170 ms) y, con la 7.6.12,
detect_tempo daba 149 (×1,5). El bombo cae a ~1 ms de una rejilla fija de 100.

Medido con los dos WAV reales (Storage, solo lectura) antes y después del arreglo:
  · «If not today…»: 99 → 99,0 · residuo 170 → 6,7 ms · constante, sin banderas.
  · «Método 2 · v1»: 149 → 99,28 · residuo 136,1 → 2,7 ms · bpm_precise 100,0 · sin banderas.
Los WAV no van al repo (34 MB cada uno): aquí se reproduce cada paso con sintéticos."""
import os
import sys

import numpy as np
import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import grid_detect as gd  # noqa: E402
import worker  # noqa: E402
from test_206_proporcion_media_rejilla import con_contratiempo  # noqa: E402


def taller(bpm=100.0, dur_s=90.0, entrada_s=19.2, sr=22050, con_bombo=True):
    """La forma del Taller: Entrada solo con hats; desde `entrada_s`, bombo en cada tiempo y
    bajo a contratiempo (en el WAV real, la mitad de los golpes graves caen a −295 ms de 600)."""
    rng = np.random.default_rng(11)
    n = int(sr * dur_s)
    y = np.zeros(n)
    beat = 60.0 / bpm
    tt = np.arange(int(0.25 * sr)) / sr
    bombo = 0.9 * np.sin(2 * np.pi * (50 + 80 * np.exp(-tt * 30)) * tt) * np.exp(-tt * 14)
    bajo = 0.8 * np.sin(2 * np.pi * 55 * tt) * np.exp(-tt * 10)
    m = int(0.02 * sr)
    hat = np.diff(rng.standard_normal(m) * np.exp(-np.arange(m) / sr * 250), prepend=0) * 0.25

    def pone(ini, s):
        i = int(round(ini * sr))
        k = min(len(s), n - i)
        if k > 0:
            y[i:i + k] += s[:k]

    for b in np.arange(0.0, dur_s, beat / 2):
        pone(b, hat)
        if con_bombo and b >= entrada_s:
            en_tiempo = abs(((b + 1e-9) / beat) - round((b + 1e-9) / beat)) < 1e-6
            pone(b, bombo if en_tiempo else bajo)
    return (y / np.max(np.abs(y)) * 0.9).astype(np.float32), sr


# ── El bombo separa los dos tempos en 3:2 ─────────────────────────────────────

def test_bombos_del_taller_en_la_rejilla_de_100_y_no_en_la_de_149():
    y, sr = taller(sr=11025)
    t, pesos = gd.ataques_de_bombo(y, sr, 60.0 / 150)
    assert len(t) >= gd.MIN_BOMBOS
    en_100 = gd.mejor_rejilla_de_bombos(t, pesos, 99.3)    # la octava gruesa del WAV real
    en_149 = gd.mejor_rejilla_de_bombos(t, pesos, 148.85)  # la proporción ×1,5 que ganaba
    assert en_100 >= en_149 * gd.VENTAJA_BOMBO, (en_100, en_149)
    assert gd.desempate_por_bombo(y, sr, 99.3, 148.85) == 99.3


def test_el_bombo_tambien_resuelve_el_136_de_206():
    import librosa
    y = librosa.resample(con_contratiempo(136), orig_sr=44100, target_sr=11025)
    assert gd.desempate_por_bombo(y, 11025, 90.67, 136.0) == 136.0


def test_sin_bombo_no_decide():
    y, sr = taller(sr=11025, con_bombo=False)
    assert gd.desempate_por_bombo(y, sr, 100.0, 150.0) is None


def test_detect_tempo_con_los_puntajes_del_taller_no_sale_149(monkeypatch):
    """Falla con la 7.6.12: librosa da 184,57 → octava 92,29 y proporciones 123,05 y 138,43.
    Con la envolvente del WAV real la búsqueda gruesa dio 99,3 (0,087) y 148,85 (0,0796): la
    octava quedaba «explicada» por la media rejilla de 148,85 y ganaba 149 con el 10 %. Aquí se
    fijan esos mismos puntajes; el audio es el sintético del Taller."""
    y, sr = taller(sr=11025)
    puntajes = {92.29: (99.3, 0.087), 123.05: (124.95, 0.0946 / 1.2), 138.43: (148.85, 0.0796)}

    def gruesa(env, sr_, dur_s, semilla, env3=None):
        return puntajes[round(semilla, 2)]

    monkeypatch.setattr(gd.librosa.beat, "beat_track", lambda **kw: (np.array([184.5703125]), None))
    monkeypatch.setattr(gd, "_busqueda_gruesa", gruesa)
    bpm, _ = gd.detect_tempo(y, sr, seed_bpm=None)
    assert abs(bpm - 100) <= 1.0, bpm


# ── Residuo del tempo sobre el bombo cuando el rastreador se pasea ─────────────

def rastreador_paseado(dur_s, desde_s=0.0):
    """Lo que hizo beat_track con el WAV real: tempo inicial 100,35 (el lag entero del
    tempograma), fase en el contratiempo y saltos de vuelta cada ~16 s."""
    t, pos, p = [], desde_s + 0.33, 60.0 / 100.35
    while pos < dur_s:
        t.append(pos)
        pos += p
        if len(t) % 27 == 0:
            pos -= 0.19
    return np.array(t)


def test_taller_constante_aunque_el_rastreador_se_pasee(monkeypatch):
    y, sr = taller()
    falsos = rastreador_paseado(len(y) / sr)

    def beat_track(**kw):
        return np.array([100.35]), librosa_frames(falsos, kw["sr"], kw["hop_length"])

    monkeypatch.setattr(worker.librosa.beat, "beat_track", beat_track)
    bpm, resid, n = worker.refine_bpm(y, sr, 100.0)
    assert worker.clasificar_tempo(resid) == "constante", (bpm, resid, n)
    assert resid <= 10.0 and abs(bpm - 100.0) <= 0.02, (bpm, resid, n)


def librosa_frames(t, sr, hop):
    import librosa
    return librosa.time_to_frames(t, sr=sr, hop_length=hop)


def test_ajuste_por_bombos_ignora_la_entrada_sin_bombo():
    y, sr = taller()
    bpm, resid, n = worker.ajuste_por_bombos(y, sr, 100.0)
    assert abs(bpm - 100.0) <= 0.02 and resid <= 10.0 and n >= 32, (bpm, resid, n)
    # Desde una semilla corrida (detect_grid daba 99 con el WAV real) también llega a 100.
    bpm99, resid99, _ = worker.ajuste_por_bombos(y, sr, 99.0)
    assert abs(bpm99 - 100.0) <= 0.02 and resid99 <= 10.0, (bpm99, resid99)


def test_tempo_que_deriva_sigue_variable():
    """Lo que tiene que seguir saliendo «variable»: un bombo que se acelera de 98 a 102."""
    sr, dur_s = 22050, 90.0
    y = np.zeros(int(sr * dur_s))
    tt = np.arange(int(0.25 * sr)) / sr
    bombo = 0.9 * np.sin(2 * np.pi * (50 + 80 * np.exp(-tt * 30)) * tt) * np.exp(-tt * 14)
    pos = 0.1
    while pos < dur_s - 0.3:
        i = int(pos * sr)
        y[i:i + len(bombo)] += bombo[:len(y) - i]
        pos += 60.0 / (98.0 + 4.0 * pos / dur_s)
    bpm, resid, n = worker.ajuste_por_bombos(y.astype(np.float32), sr, 100.0)
    assert resid is None or worker.clasificar_tempo(resid) == "variable", (bpm, resid, n)


def test_si_el_rastreador_ya_da_constante_no_se_toca(monkeypatch):
    y, sr = taller(dur_s=40)
    monkeypatch.setattr(worker, "_ajuste_por_rastreador", lambda *a: (100.002, 4.0, 60))
    monkeypatch.setattr(worker, "ajuste_por_bombos", lambda *a: pytest.fail("no debía correr"))
    assert worker.refine_bpm(y, sr, 100.0) == (100.002, 4.0, 60)


# ── Banderas ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bpm, fuera", [(99.959, False), (99.999, False), (150.3, False),
                                         (99.2, True), (151.0, True), (164.2, True)])
def test_cien_exactos_no_esta_fuera_de_rango(bpm, fuera):
    problemas, _ = worker.sanity_check({"bpm_precise": bpm}, 300000)
    assert any(p.startswith("bpm_fuera_de_rango") for p in problemas) == fuera
