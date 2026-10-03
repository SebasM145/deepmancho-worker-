"""Funciones puras de stems_worker (W6, noche 2→3-oct): patrones, loops y render.

Nada de esto toca la red, ffmpeg, demucs ni la base: señales sintéticas con
numpy. Fija el comportamiento actual para que un cambio lo rompa en CI y no en
la biblioteca de un DJ.
"""
import os
import sys

import numpy as np
import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stems_worker as sw  # noqa: E402

SR = 22050


def tono(hz, dur_s, sr=SR, amp=0.5):
    t = np.arange(int(dur_s * sr)) / sr
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def bombos(bpm, desfase_s=0.0, dur_s=20.0, sr=SR):
    """Un golpe grave (60 Hz que decae) en cada tiempo, corrido `desfase_s`."""
    y = np.zeros(int(dur_s * sr), dtype=np.float32)
    golpe = tono(60, 0.12, sr, 0.9) * np.exp(-np.arange(int(0.12 * sr)) / (0.03 * sr)).astype(np.float32)
    t = desfase_s
    while t + 0.12 < dur_s:
        i = int(t * sr)
        y[i:i + len(golpe)] += golpe
        t += 60.0 / bpm
    return y


# ───────────────────────────── rejilla y ancla ─────────────────────────────
def test_beat_grid():
    ms, to_beat = sw.beat_grid(120, 250)
    assert ms == 500.0
    assert to_beat(0.25) == 0.0
    assert to_beat(0.75) == 1.0
    assert to_beat(0.0) == -0.5


def test_refine_anchor_corrige_hacia_el_bombo():
    beat = 60.0 / 124
    kicks = [0.020 + i * beat for i in range(32)]   # los bombos caen 20 ms después del ancla
    assert sw.refine_anchor(kicks, 124, 0.0) == pytest.approx(0.020, abs=1e-6)


def test_refine_anchor_corrige_hacia_atras():
    beat = 60.0 / 124
    kicks = [beat - 0.030 + i * beat for i in range(32)]   # 30 ms ANTES del pulso
    assert sw.refine_anchor(kicks, 124, 0.0) == pytest.approx(-0.030, abs=1e-6)


def test_refine_anchor_no_toca_con_pocos_golpes_o_sin_consenso():
    assert sw.refine_anchor([0.1, 0.6, 1.1], 120, 0.0) == 0.0
    rng = np.random.RandomState(3)
    dispersos = sorted(rng.uniform(0, 30, 64))
    assert sw.refine_anchor(dispersos, 120, 0.0) == 0.0


def test_refine_anchor_no_toca_si_ya_esta_bien():
    beat = 60.0 / 120
    kicks = [0.002 + i * beat for i in range(16)]   # 2 ms: por debajo del umbral de 5 ms
    assert sw.refine_anchor(kicks, 120, 0.0) == 0.0


# ───────────────────────────── perfil de sonido ─────────────────────────────
def test_decay_ms_y_tramos_cortos():
    assert sw._decay_ms(np.zeros(10), SR) == 0.0
    golpe = tono(100, 0.5) * np.exp(-np.arange(int(0.5 * SR)) / (0.05 * SR))
    largo = tono(100, 0.5) * np.exp(-np.arange(int(0.5 * SR)) / (0.15 * SR))
    assert 0 < sw._decay_ms(golpe, SR) < sw._decay_ms(largo, SR)


def test_dominant_y_centroide():
    assert sw._dominant_hz(tono(55, 1.0), SR) == pytest.approx(55, abs=1.5)
    assert sw._dominant_hz(np.zeros(100), SR) == 0.0
    assert sw._centroid_hz(np.zeros(100), SR) == 0.0
    assert sw._centroid_hz(np.zeros(4096), SR) == 0.0   # silencio: sin división por cero
    assert sw._centroid_hz(tono(4000, 0.5), SR) > sw._centroid_hz(tono(400, 0.5), SR)


def test_band_ratio():
    sub = tono(50, 0.5)
    assert sw._band_ratio(sub, SR, 30, 80) > 0.9
    assert sw._band_ratio(sub, SR, 2000, 6000) < 0.01
    assert sw._band_ratio(np.zeros(32), SR, 30, 80) == 0.0
    # un clic de 12 ms también se mide (relleno con ceros)
    assert sw._band_ratio(tono(3000, 0.012), SR, 2000, 6000) > 0.5


def test_cutoff_hz():
    assert sw._cutoff_hz(np.zeros(100), SR) == 0.0
    grave = sw._cutoff_hz(tono(200, 1.0), SR)
    agudo = sw._cutoff_hz(tono(2000, 1.0), SR)
    assert grave < 300 < agudo


def test_highpass_quita_el_grave():
    y = tono(60, 0.5) + tono(6000, 0.5, amp=0.1)
    hp = sw._highpass(y, SR, 3000)
    assert sw._band_ratio(hp, SR, 30, 200) < sw._band_ratio(y, SR, 30, 200) / 5


def test_snap_to_peak():
    y = np.zeros(SR, dtype=np.float32)
    y[int(0.512 * SR)] = 1.0
    assert sw._snap_to_peak(y, SR, 0.5) == pytest.approx(0.512, abs=1 / SR)
    assert sw._snap_to_peak(y[:10], SR, 0.0) == 0.0   # ventana muy chica: devuelve lo teórico


def test_hits_recorta_alrededor_de_cada_golpe():
    y = bombos(120, dur_s=4)
    trozos = sw._hits(y, SR, [0.0, 0.5, 1.0], post=0.2, snap=False)
    assert len(trozos) == 3
    assert all(abs(len(t) - 0.2 * SR) <= 1 for t in trozos)
    assert len(sw._hits(y, SR, [0.5] * 50, post=0.2)) == 40   # tope de 40 golpes
    assert sw._hits(y, SR, [3.999], post=0.2, snap=False) == []   # al final no alcanza


# ───────────────────────────── bloques ─────────────────────────────
def nota(b, n, d=0.25, v=0.8):
    return {"b": b, "n": n, "d": d, "v": v}


def riff(desde_beat, variacion=False):
    notas = [nota(desde_beat + x, 36) for x in (0, 1, 2, 3)] + [nota(desde_beat + 0.5, 43)]
    if variacion:
        notas.append(nota(desde_beat + 3.5, 48))   # nota fantasma: sigue siendo el riff
    return notas


def test_huella_ignora_microcorrimientos_y_fuerza():
    a = [nota(0.0, 36, v=0.2), nota(1.0, 40)]
    b = [nota(0.01, 36, v=0.9), nota(0.99, 40)]
    assert sw._huella(a) == sw._huella(b)
    assert sw._huella(a) != sw._huella([nota(0.5, 36), nota(1.0, 40)])


def test_similitud():
    assert sw._similitud(set(), set()) == 1.0
    assert sw._similitud({1}, set()) == 0.0
    assert sw._similitud({1, 2, 3}, {1, 2, 4}) == pytest.approx(0.5)


def test_notes_in_lleva_a_cero():
    out = sw._notes_in([nota(3.5, 36), nota(4.0, 38), nota(8.0, 40)], 4, 8)
    assert out == [{"b": 0.0, "n": 38, "d": 0.25, "v": 0.8}]


def test_bloques_de_agrupa_por_parecido():
    notas = []
    for bar in range(8):
        notas += riff(bar * 4, variacion=(bar % 3 == 0))
    bloques = sw.bloques_de(notas, 8, largos=(1,))
    assert len(bloques) == 1
    b = bloques[0]
    assert b["repite"] == 8 and b["cubre"] == 1.0
    assert b["aparece_en"] == list(range(1, 9))
    # el representativo es la versión típica (sin la nota fantasma)
    assert len(b["notas"]) == 5


def test_bloques_de_sin_repeticion_o_vacio():
    assert sw.bloques_de([], 8) == []
    assert sw.bloques_de(riff(0), 0) == []
    unico = riff(0)                                # una sola vez: no es bloque
    assert sw.bloques_de(unico, 4, largos=(1,)) == []


def test_bloques_de_respeta_el_tope_y_el_orden():
    notas = []
    for bar in range(16):
        notas += riff(bar * 4)
    bloques = sw.bloques_de(notas, 16, tope=2)
    assert len(bloques) == 2
    assert bloques[0]["cubre"] >= bloques[1]["cubre"]
    assert bloques[0]["bars"] <= bloques[1]["bars"]   # a igual cobertura, el más corto primero


def test_bloque_bateria():
    four = [1, 0, 0, 0] * 4
    hat = [0, 0, 1, 0] * 4
    grid = {"kick": [four] * 6, "snare": [[0] * 16] * 6, "hat": [hat] * 4 + [[0] * 16] * 2}
    out = sw.bloque_bateria(grid)
    assert [b["repite"] for b in out] == [4, 2]
    assert out[0]["aparece_en"] == [1, 2, 3, 4]
    assert out[1]["desde_compas"] == 5
    assert out[0]["cubre"] == pytest.approx(0.667)
    assert sw.bloque_bateria({}) == []
    assert sw.bloque_bateria({"kick": [four]}) == []   # una sola vez no es bloque


# ───────────────────────────── notas sueltas / sampler ─────────────────────────────
def test_notas_sueltas_elige_la_aislada_y_larga():
    seq = [
        nota(0, 60, d=1.0, v=0.5),
        nota(0.5, 64, d=1.0),          # se pisa con la anterior: ninguna de las dos está sola
        nota(8, 62, d=0.5, v=0.4),
        nota(16, 62, d=1.0, v=0.3),    # la más larga de las 62
        nota(24, 67, d=0.1),           # muy corta: no sirve
    ]
    out = sw.notas_sueltas(seq, 120, 1000)
    assert set(out) == {62}
    assert out[62] == {"n": 62, "t": 9.0, "dur": 0.5, "v": 0.3}
    assert sw.notas_sueltas([], 120, 0) == {}


@pytest.mark.parametrize("alturas,clonable", [
    ([], "no"),
    (list(range(48, 72, 2)), "bien"),          # 12 alturas, huecos de 2
    ([40, 45, 50, 55, 60], "a_medias"),
    ([40, 60, 80, 81, 82], "no"),              # hueco de 20 semitonos
])
def test_resumen_sampler(alturas, clonable):
    r = sw.resumen_sampler({a: {} for a in alturas})
    assert r["clonable"] == clonable
    assert r["alturas"] == len(alturas)
    if alturas:
        assert r["rango"] == [min(alturas), max(alturas)]


# ───────────────────────────── loops ─────────────────────────────
def test_compas_s():
    assert sw.compas_s(120) == 2.0
    assert sw.compas_s(125) == pytest.approx(1.92)


def test_rms():
    assert sw._rms([]) == 0.0
    assert sw._rms(np.ones(100) * 0.5) == pytest.approx(0.5)


def test_golpes_de_bombo_s_ignora_los_hats():
    y = bombos(120, 0.1, dur_s=8)
    # hats en contratiempo, agudos y fuertes: no son bombo
    hat = (np.random.RandomState(1).randn(int(0.02 * SR)) * 0.8).astype(np.float32)
    for k in range(16):
        i = int((0.1 + 0.25 + k * 0.5) * SR)
        y[i:i + len(hat)] += hat
    golpes = sw.golpes_de_bombo_s(y, SR)
    assert len(golpes) == 16                     # 0,1 s … 7,6 s, uno por tiempo
    desvio = [((t - 0.1) % 0.5) for t in golpes]
    assert max(min(d, 0.5 - d) for d in desvio) < 0.01
    assert sw.golpes_de_bombo_s(np.zeros(100), SR) == []


def test_onsets_s():
    y = np.zeros(SR * 2, dtype=np.float32)
    for t in (0.5, 1.0, 1.5):
        i = int(t * SR)
        y[i:i + 300] = np.random.RandomState(int(t * 10)).randn(300) * 0.8
    golpes = sw.onsets_s(y, SR)
    # cada ráfaga se detecta, y nada fuera de ellas (dentro de una ráfaga puede marcar más de un pico)
    for t in (0.5, 1.0, 1.5):
        assert any(abs(g - t) < 0.01 for g in golpes)
    assert all(min(abs(g - t) for t in (0.5, 1.0, 1.5)) < 0.03 for g in golpes)
    assert sw.onsets_s(np.zeros(100), SR) == []


def test_controlar_loop_aprueba_bateria_en_rejilla():
    y = bombos(120, 0.0, dur_s=40)
    ok, qc = sw.controlar_loop(y, SR, 120, 0.0, 1, "drums")
    assert ok, qc
    assert qc["alineacion_ms"] < 5
    assert len(qc["rms_compases"]) == 8


def test_controlar_loop_rechaza_huecos_y_fuera_del_audio():
    y = bombos(120, 0.0, dur_s=40)
    hueco = y.copy()
    hueco[int(6 * SR):int(10 * SR)] = 0          # compases 4 y 5 en silencio
    ok, qc = sw.controlar_loop(hueco, SR, 120, 0.0, 1, "drums")
    assert not ok and qc["motivo"].startswith("hueco")
    ok, qc = sw.controlar_loop(y, SR, 120, 0.0, 17, "drums")   # 17 + 8 compases = 48 s > 40 s
    assert not ok and qc["motivo"] == "fuera del audio"


def test_controlar_loop_rechaza_bombo_corrido():
    y = bombos(120, 0.040, dur_s=40)                # 40 ms tarde respecto de la fase dada
    ok, qc = sw.controlar_loop(y, SR, 120, 0.0, 1, "drums")
    assert not ok and qc["motivo"].startswith("bombo corrido")


def test_controlar_loop_rechaza_empalme_desparejo():
    y = tono(220, 40.0, amp=0.3)
    y[int(16 * SR):] *= 0.2                         # después del loop la pista baja mucho
    ok, qc = sw.controlar_loop(y, SR, 120, 0.0, 1, "bass")
    assert not ok and qc["motivo"].startswith("empalme desparejo")
    ok, qc = sw.controlar_loop(tono(220, 40.0, amp=0.3), SR, 120, 0.0, 1, "bass")
    assert ok and "alineacion_ms" not in qc        # la alineación solo se mide en batería


def test_cortar_loops_usa_la_fase_medida_y_respeta_la_calidad():
    y_dr = bombos(120, 0.030, dur_s=70)             # la fase real es 30 ms, la dada 0
    y_bass = tono(55, 70.0, amp=0.3)
    track = {"bpm": 120, "phase_ms": 0, "bars": 32}
    lanes = {"drums": [{"from": 1, "to": 32}], "bass": [{"from": 1, "to": 32}],
             "vocals": [{"from": 1, "to": 32}]}
    energia = {"drums": [1.0] * 32, "bass": [1.0] * 32}
    calidad = {"drums": {"estado": "limpia"}, "bass": {"estado": "con_algo"}, "vocals": {"estado": "mezclada"}}
    audio = {"drums": (y_dr, SR), "bass": (y_bass, SR), "vocals": (y_bass, SR)}
    loops, rechazos = sw.cortar_loops(track, lanes, energia, calidad, audio)
    por_stem = {}
    for lp in loops:
        por_stem.setdefault(lp["stem"], []).append(lp["from_bar"])
    # ventanas 1, 9, 17, 25 → separación mínima de 16 compases: 1 y 17
    assert por_stem == {"drums": [1, 17], "bass": [1, 17]}
    assert rechazos == {"pista no limpia": 1}
    dr = next(lp for lp in loops if lp["stem"] == "drums")
    assert dr["role"] == "bateria"
    assert dr["qc"]["fase_medida_ms"] == pytest.approx(30, abs=3)
    assert dr["qc"]["fase_dada_ms"] == 0
    bajo = next(lp for lp in loops if lp["stem"] == "bass")
    assert bajo["role"] == "bajo" and "key_detectada" in bajo["qc"]


# ───────────────────────────── render ─────────────────────────────
def test_compas_a_muestra():
    assert sw.compas_a_muestra(1, 120, 0, 1000) == 0
    assert sw.compas_a_muestra(3, 120, 250, 1000) == 4250


def test_rampa_db():
    assert np.all(sw.rampa_db(10, [], 120, 1) == 1)
    g = sw.rampa_db(4 * 1000, [(1, 0.0), (2, -20.0)], 120, 1, sr=1000)
    assert g[0] == pytest.approx(1.0)
    assert g[1000] == pytest.approx(10 ** (-10 / 20), rel=1e-3)   # a mitad de compás, −10 dB
    assert g[-1] == pytest.approx(0.1, rel=1e-3)                  # después del último punto, se queda


def test_paneo_potencia_constante():
    mono = np.ones(100, dtype=np.float32)
    for pos in (-1.0, -0.3, 0.0, 0.7, 1.0):
        st = sw.paneo(mono, pos)
        assert np.allclose(st[0] ** 2 + st[1] ** 2, 1.0, atol=1e-6)
    izq = sw.paneo(mono, -1.0)
    assert np.allclose(izq[1], 0, atol=1e-6)
    movil = sw.paneo(mono, sw.lfo_paneo(100, 10, 0.5, sr=1000))
    assert movil.shape == (2, 100)


def test_tiro_de_eco():
    st = np.zeros((2, 1000), dtype=np.float32)
    st[:, 100] = 1.0
    st[:, 500] = 1.0
    out = sw.tiro_de_eco(st, 400, 100, 2, sr=1000)
    assert out.shape == (2, 1200)
    assert out[0, 100] == 1.0 and out[0, 200] == 0.0      # antes de `desde` no hay eco
    assert out[0, 600] == pytest.approx(0.5 * 0.6)        # primera vuelta: −6 dB y más oscura
    assert out[0, 700] == pytest.approx(0.25 * 0.6)


def test_mezclar_ubica_cada_pista_en_su_compas():
    sr = 1000
    pistas = [{"audio": np.ones(10), "desde_compas": 2},
              {"audio": np.ones(10), "desde_compas": 3, "ganancia_db": -6.0206, "paneo": 1.0}]
    mix = sw.mezclar(pistas, 120, 4, sr=sr)
    assert mix.shape == (2, sw.compas_a_muestra(5, 120, 0, sr) + sr * 4)
    c = np.cos(np.pi / 4)
    assert mix[0, 2000] == pytest.approx(c) and mix[0, 1999] == 0
    assert mix[1, 4000] == pytest.approx(0.5, abs=1e-4) and mix[0, 4000] == pytest.approx(0, abs=1e-6)


def test_masterizar_nunca_pasa_el_techo():
    mix = np.array([[2.0, -3.0], [0.5, 1.0]], dtype=np.float32)
    out = sw.masterizar(mix)
    assert np.max(np.abs(out)) == pytest.approx(10 ** (-1 / 20), rel=1e-5)
    bajo = np.array([[0.1, -0.2]], dtype=np.float32)
    assert sw.masterizar(bajo) is bajo


def test_limitador_y_master_club():
    rng = np.random.RandomState(5)
    st = (rng.randn(2, 44100) * 0.03).astype(np.float32)   # bajo: ≈ −28 dB
    st[:, 20000] = 4.0
    techo = 10 ** (-1 / 20)
    out = sw.limitador(st)
    assert out.shape == st.shape
    assert np.max(np.abs(out)) <= techo + 1e-6
    club = sw.master_club(st, objetivo_db=-9.0)
    assert np.max(np.abs(club)) <= techo + 1e-6
    assert sw.sonoridad_aprox_db(club) > sw.sonoridad_aprox_db(st)


def test_limpiar_graves():
    y = tono(40, 1.0, sr=44100) + tono(1000, 1.0, sr=44100, amp=0.2)
    assert sw.limpiar_graves(y, 0) is y
    out = sw.limpiar_graves(y, 150)
    assert sw._band_ratio(out, 44100, 20, 80) < 0.01
    assert sw._band_ratio(out, 44100, 900, 1100) > 0.95


def test_respiro_baja_en_el_tiempo_y_se_recupera():
    sr = 1000
    g = sw.respiro(2000, 120, 0, profundidad_db=4.0, sr=sr)
    assert g[0] == pytest.approx(10 ** (-4 / 20), rel=1e-4)
    assert g[250] > 10 ** (-0.2 / 20)                   # a medio tiempo ya volvió
    assert g[500] == pytest.approx(g[0], rel=1e-4)


def test_respuesta_sala_es_siempre_la_misma():
    a = sw.respuesta_sala(0.1, sr=1000)
    b = sw.respuesta_sala(0.1, sr=1000)
    assert np.array_equal(a, b) and a.shape == (2, 100)


def test_convolucion_con_impulso_es_identidad():
    st = np.random.RandomState(2).randn(2, 50).astype(np.float32)
    ir = np.zeros((2, 5), dtype=np.float32)
    ir[:, 0] = 1.0
    out = sw.convolucion(st, ir)
    assert out.shape == (2, 54)
    assert np.allclose(out[:, :50], st, atol=1e-5)


def test_mezclar_ficha_estereo_y_sala():
    sr = 1000
    est = np.stack([np.ones(10), np.zeros(10)]).astype(np.float32)
    mix = sw.mezclar_ficha([{"audio": est, "desde_compas": 1}], 120, 2, sr=sr)
    assert mix[0, 0] == 1.0 and mix[1, 0] == 0.0          # estéreo sin paneo: se respeta
    con_sala = sw.mezclar_ficha([{"audio": est, "desde_compas": 1, "espacio_db": 0}], 120, 2, sr=sr)
    assert np.abs(con_sala[:, 50:]).sum() > 0             # la cola de la sala suena después
    assert np.abs(mix[:, 50:]).sum() == 0
