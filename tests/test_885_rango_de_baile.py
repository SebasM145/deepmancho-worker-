"""dj-connect #885 (10-oct): en 48 temas con el BPM bloqueado (Mixed In Key o a mano), el
`bpm_detected` guardado estaba a 3/4, 5/4, 4/3 o 3/2 del tempo real. Medido con su audio (el
stream MP3 público, solo lectura) y el detector de 7.6.20: 34 ya salían bien (lo guardado es de
versiones viejas) y 14 no. Tres formas:
  · 186 por 124 y 184,5 por 123 (3:2), 151,25 por 121 (5:4): la octava (124) estaba dentro del
    rango de baile y una proporción de afuera le ganaba. En `0d474d0a` el bajo hace semicorcheas
    (0, ¼ y ¾ de cada tiempo) en la banda del bombo con la misma fuerza, y el desempate por el
    bombo elegía 186 aunque la rejilla de ataques prefería 124 (0,466 contra 0,362).
  · 91,5 por 122 (`d91a7c9d`): 122 ni siquiera era candidata; la rejilla de 122 junta el 88 % de
    los bombos y la de 91,5 el 23 %.
  · 6 temas tienen la ETIQUETA mal (90–97 en house, o 123 en un tema de 120): el bombo le da la
    razón al detector (71–94 % contra 9–25 %). No es del worker.
Las pruebas fijan los puntajes medidos en el audio real (que no va al repo) y corren el detector de
verdad, como test_618_taller_tempo."""
import os
import sys

import numpy as np
import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import grid_detect as gd  # noqa: E402
from test_618_taller_tempo import taller  # noqa: E402


def _fija(monkeypatch, cruda, puntajes, bombo, fraccion=None):
    """librosa da `cruda`; la búsqueda gruesa devuelve `puntajes[semilla redondeada]`, la búsqueda
    cerca de una proporción devuelve `puntajes['cerca'](centro)` y el desempate por el bombo lo
    decide `bombo(a, b)`."""
    def gruesa(env, sr_, dur_s, semilla, env3=None):
        return puntajes[round(semilla, 2)]

    monkeypatch.setattr(gd.librosa.beat, "beat_track", lambda **kw: (np.array([cruda]), None))
    monkeypatch.setattr(gd, "_busqueda_gruesa", gruesa)
    monkeypatch.setattr(gd, "_busqueda_cerca", lambda env, sr_, dur_s, c, env3, margen=0.02: puntajes["cerca"](c))
    monkeypatch.setattr(gd, "desempate_por_bombo", lambda y, sr, a, b: bombo(a, b))
    if fraccion:
        monkeypatch.setattr(gd, "mejor_rejilla_de_bombos", lambda t, p, bpm: fraccion(bpm))


def _sin_cerca(semilla):
    raise AssertionError(f"no se esperaba buscar cerca de {semilla}")


def test_rango_de_baile_es_el_de_bpm_fuera_de_rango():
    assert gd.en_rango_de_baile(99.5) and gd.en_rango_de_baile(150.5) and gd.en_rango_de_baile(124)
    assert not gd.en_rango_de_baile(186) and not gd.en_rango_de_baile(91.5)
    assert not gd.en_rango_de_baile(None)
    assert gd.saca_del_rango(124, 186)
    assert not gd.saca_del_rango(160, 128)       # #206: de afuera hacia adentro sí compite
    assert gd.saca_del_rango(124, 93)


def test_el_bombo_no_saca_124_a_186(monkeypatch):
    """`0d474d0a` (Tech House, 124): con 7.6.20 salía 186. Puntajes medidos en su audio."""
    y, sr = taller(bpm=124.0, sr=11025)
    _fija(monkeypatch, 129.19921875,
          {129.2: (124.0133, 0.4657), 172.27: (185.9844, 0.3619), 96.9: (92.9975, 0.3361),
           "cerca": _sin_cerca},
          bombo=lambda a, b: max(a, b))           # el bajo rodante engaña al bombo hacia arriba
    bpm, _ = gd.detect_tempo(y, sr, seed_bpm=None)
    assert round(bpm) == 124, bpm


def test_la_media_rejilla_no_saca_123_a_184(monkeypatch):
    """`1289fd09` (123): la regla de #206 (octava explicada por la media rejilla de la proporción,
    con 10 %) también lo sacaba del rango. Sin bombo que decida."""
    y, sr = taller(bpm=123.0, sr=11025)
    _fija(monkeypatch, 129.19921875,
          {129.2: (123.0133, 0.2765), 172.27: (184.4844, 0.2577), 96.9: (92.2475, 0.2857),
           "cerca": _sin_cerca},
          bombo=lambda a, b: None)
    bpm, _ = gd.detect_tempo(y, sr, seed_bpm=None)
    assert round(bpm) == 123, bpm


def test_el_bombo_no_saca_121_a_151(monkeypatch):
    """`40b17abb` (Afro House, 121): 5:4, salía 151,25."""
    y, sr = taller(bpm=121.0, sr=11025)
    _fija(monkeypatch, 117.45383522727273,
          {117.45: (121.0075, 0.3669), 176.18: (181.4863, 0.3016), 156.61: (151.2767, 0.3342),
           "cerca": _sin_cerca},
          bombo=lambda a, b: max(a, b))
    bpm, _ = gd.detect_tempo(y, sr, seed_bpm=None)
    assert round(bpm) == 121, bpm


def test_el_bombo_trae_91_5_de_vuelta_a_122(monkeypatch):
    """`d91a7c9d` (Deep House, 122): la octava quedaba en 91,5 y 122 (×4/3) no era candidata."""
    y, sr = taller(bpm=122.0, sr=11025)
    _fija(monkeypatch, 99.38401442307692,
          {99.38: (91.4833, 0.386), 149.08: (152.4999, 0.3455), 132.51: (137.2611, 0.3173),
           "cerca": lambda c: (round(c), 0.3251)},
          bombo=lambda a, b: 122 if 122 in (a, b) else None,
          fraccion=lambda bpm: 0.883 if round(bpm) == 122 else 0.23)
    bpm, _ = gd.detect_tempo(y, sr, seed_bpm=None)
    assert round(bpm) == 122, bpm


def test_por_encima_del_rango_no_se_busca_hacia_abajo(monkeypatch):
    """Un 160 de verdad (la octava ya era 160) sigue en 160: arriba del rango no se buscan
    proporciones (`cerca` no se llama)."""
    y, sr = taller(bpm=160.0, sr=11025)
    _fija(monkeypatch, 161.4990234375,
          {161.5: (160.0, 0.5038), 107.67: (106.58, 0.5793 / 1.2), 121.12: (119.85, 0.3866),
           "cerca": _sin_cerca},
          bombo=lambda a, b: 160.0 if 160.0 in (a, b) else None)
    bpm, _ = gd.detect_tempo(y, sr, seed_bpm=None)
    assert round(bpm) == 160, bpm


def test_con_semilla_no_se_toca(monkeypatch):
    """Con BPM conocido (semilla) el detector no busca proporciones ni vuelve al rango."""
    y, sr = taller(bpm=100.0, sr=11025)
    llamado = []
    monkeypatch.setattr(gd, "volver_al_rango_por_bombo", lambda *a, **k: llamado.append(1) or a[3])
    gd.detect_tempo(y, sr, seed_bpm=172.0)
    assert not llamado


@pytest.mark.parametrize("bpm", [100.0, 124.0, 150.0, 160.0, 186.0])
def test_volver_al_rango_solo_mira_hacia_arriba_desde_abajo(bpm):
    assert gd.volver_al_rango_por_bombo(None, None, None, 0, (bpm, 0.3), None) == (bpm, 0.3)
