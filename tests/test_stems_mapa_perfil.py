"""stems_worker: mapa del arreglo, rejilla de batería, acordes, perfil de sonido
y apagado (W6, relevo de noche 4-oct).

Estas funciones llenan `track_patterns` (lo que ve el DJ en «Patrones» y lo que
usa el corte de loops) y no tenían prueba. Sin red, sin ffmpeg y sin demucs:
`to_wav_mono` se reemplaza por señales sintéticas de numpy y `requests` por un
espía. Fijan el comportamiento actual.
"""
import os
import sys
from pathlib import Path

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


@pytest.fixture
def audios(monkeypatch):
    """`to_wav_mono(path)` devuelve la señal guardada con ese nombre, remuestreada
    a lo que pida la función (solo hace falta para stem_stats, que lee a 11025)."""
    banco = {}

    def falso(path, sr=22050):
        y = banco[str(path)]
        if isinstance(y, Exception):
            raise y
        if sr != SR:
            y = y[:: SR // sr]
        return y.astype(np.float32), sr

    monkeypatch.setattr(sw, "to_wav_mono", falso)
    return banco


# ─────────────────────────────── mapa del arreglo ───────────────────────────────
def test_mapa_arreglo_marca_entradas_y_salidas_por_compas(audios):
    # 120 BPM → compás de 2 s. 16 compases: el bajo entra en el 5 y sale tras el 12.
    bpm, compas = 120, 2.0
    bajo = np.zeros(int(32 * SR), dtype=np.float32)
    bajo[int(4 * compas * SR): int(12 * compas * SR)] = tono(55, 8 * compas)
    audios["bass"] = bajo
    m = sw.mapa_arreglo({"bass": "bass"}, bpm, 0, 32.0)
    assert m["compases"] == 16
    b = m["instrumentos"]["bass"]
    assert b["tramos"] == [{"desde": 5, "hasta": 12}]
    assert len(b["energia"]) == 16
    assert max(b["energia"]) == 1.0 and b["energia"][0] == 0.0


def test_mapa_arreglo_ignora_entradas_de_menos_de_4_compases(audios):
    y = np.zeros(int(32 * SR), dtype=np.float32)
    y[int(2 * SR): int(8 * SR)] = tono(440, 6)       # 3 compases: no es una entrada
    y[int(16 * SR):] = tono(440, 16)                  # del 9 al final: sí, abierta hasta el último
    audios["other"] = y
    m = sw.mapa_arreglo({"other": "other"}, 120, 0, 32.0)
    assert m["instrumentos"]["other"]["tramos"] == [{"desde": 9, "hasta": 16}]


def test_mapa_arreglo_umbral_relativo_al_pico_de_cada_pista(audios):
    # Un pad muy bajo (−40 dB) que suena todo el tema también cuenta como que suena.
    audios["pad"] = tono(220, 32, amp=0.005)
    m = sw.mapa_arreglo({"pad": "pad"}, 120, 0, 32.0)
    assert m["instrumentos"]["pad"]["tramos"] == [{"desde": 1, "hasta": 16}]


def test_mapa_arreglo_respeta_el_ancla(audios):
    # Con el ancla en 1 s, el compás 1 empieza en 1 s y entran 15 compases.
    y = np.zeros(int(32 * SR), dtype=np.float32)
    y[int(1 * SR): int(9 * SR)] = tono(55, 8)
    audios["bass"] = y
    m = sw.mapa_arreglo({"bass": "bass"}, 120, 1000, 32.0)
    assert m["compases"] == 15
    assert m["instrumentos"]["bass"]["tramos"] == [{"desde": 1, "hasta": 4}]


def test_mapa_arreglo_omite_pistas_mudas_o_ilegibles(audios):
    audios["vocals"] = np.zeros(int(32 * SR), dtype=np.float32)
    audios["drums"] = RuntimeError("ffmpeg falló")
    audios["bass"] = tono(55, 32)
    m = sw.mapa_arreglo({"vocals": "vocals", "drums": "drums", "bass": "bass"}, 120, 0, 32.0)
    assert set(m["instrumentos"]) == {"bass"}


def test_mapa_arreglo_tema_mas_corto_que_un_compas(audios):
    audios["bass"] = tono(55, 1)
    m = sw.mapa_arreglo({"bass": "bass"}, 120, 0, 1.0)
    assert m["compases"] == 1                         # nunca 0: la pantalla divide por esto


# ───────────────────────────────── estadísticas ─────────────────────────────────
def test_stem_stats(audios):
    audios["drums"] = tono(100, 4, amp=1.0)
    audios["vocals"] = np.zeros(0, dtype=np.float32)
    notas = [{"n": 36}, {"n": 43}, {"n": 31}]
    rejilla = {"bars": 2, "kick": [[1, 0, 0, 0] * 4, [1, 0, 0, 0] * 4],
               "snare": [[0] * 16, [0] * 4 + [1] + [0] * 11], "hat": [[0, 0, 1, 0] * 4] * 2}
    st = sw.stem_stats({"drums": "drums", "vocals": "vocals"}, notas, rejilla)
    assert st["rms_drums"] == pytest.approx(1 / np.sqrt(2), abs=1e-3)
    assert st["rms_vocals"] == 0.0                    # pista vacía: 0, sin dividir por cero
    assert st["bass_range"] == [31, 43] and st["bass_notes"] == 3
    assert st["kick_density"] == 0.25
    assert st["snare_density"] == pytest.approx(1 / 32, abs=1e-3)
    assert st["hat_density"] == 0.25


def test_stem_stats_sin_notas_ni_compases(audios):
    audios["bass"] = tono(55, 1)
    st = sw.stem_stats({"bass": "bass"}, [], {"bars": 0})
    assert set(st) == {"rms_bass"}


# ─────────────────────────────────── acordes ───────────────────────────────────
def test_plantillas_de_acordes():
    t = sw.chord_templates()
    assert len(t) == 24
    nombres = dict(t)
    assert set(np.flatnonzero(nombres["C"])) == {0, 4, 7}
    assert set(np.flatnonzero(nombres["Am"])) == {9, 0, 4}
    assert set(np.flatnonzero(nombres["F#"])) == {6, 10, 1}
    assert all(v.sum() == 3 for _, v in t)
    assert sw.chord_templates() is t                  # se arman una sola vez


def test_chords_per_bar_reconoce_la_triada(audios):
    # La menor (la-do-mi) los 2 primeros compases y Do mayor (do-mi-sol) el 3.º y el 4.º.
    am = tono(220.0, 4) + tono(261.63, 4) + tono(329.63, 4)
    c = tono(261.63, 4) + tono(329.63, 4) + tono(392.0, 4)
    audios["other"] = np.concatenate([am, c])
    acordes = sw.chords_per_bar(["other"], 120, 0, 4)
    assert [a["bar"] for a in acordes] == [0, 1, 2, 3]
    assert [a["chord"] for a in acordes] == ["Am", "Am", "C", "C"]
    assert all(0.8 < a["conf"] <= 1.0 for a in acordes)


def test_chords_per_bar_suma_pistas_y_corta_al_audio(audios):
    # Dos pistas de largo distinto: se mezclan hasta la más corta, y los compases
    # que quedan fuera del audio no aparecen.
    audios["a"] = tono(220.0, 3) + tono(329.63, 3)
    audios["b"] = tono(261.63, 6)
    acordes = sw.chords_per_bar(["a", "b"], 120, 0, 8)
    assert acordes and acordes[0]["chord"] == "Am"
    assert max(a["bar"] for a in acordes) <= 1


def test_chords_per_bar_sin_pistas_o_sin_compases(audios):
    audios["x"] = tono(220, 2)
    assert sw.chords_per_bar([], 120, 0, 4) == []
    assert sw.chords_per_bar(["x"], 120, 0, 0) == []


# ─────────────────────────────── rejilla de batería ───────────────────────────────
def banda(x, lo, hi, sr=SR):
    """Filtro ideal por FFT: deja solo lo que está entre `lo` y `hi` Hz."""
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / sr)
    X[(f < lo) | (f >= hi)] = 0
    return np.fft.irfft(X, len(x))


def bateria(bpm, compases, desfase_s=0.0, sr=SR, con_clic=True, con_hats=True):
    """Bombo de 55 Hz (con clic en 2,5–4,5 kHz) en cada tiempo y hat (8–11 kHz) en cada contratiempo.

    El clic y los hats se filtran sobre la pista entera, no golpe por golpe: un
    trozo filtrado y recortado deja bordes que suenan en todas las bandas.
    """
    rng = np.random.default_rng(3)
    beat = 60.0 / bpm
    largo = int((compases * 4 * beat + 1) * sr)
    # Piso de ruido (−50 dB), como en una pista separada de verdad: sin él, el
    # detector mide en dB contra silencio digital y cualquier fuga parece un golpe.
    y = 0.003 * rng.standard_normal(largo)
    clics, hats = np.zeros(largo), np.zeros(largo)
    n, m = int(0.4 * sr), int(0.05 * sr)
    t = np.arange(n) / sr
    # Ataque de 8 ms y cola completa: un grave solo, sin bordes, no trae clic.
    bombo = 0.9 * np.sin(2 * np.pi * 55 * t) * np.exp(-t / 0.05) * np.minimum(1.0, t / 0.008)
    for i in range(compases * 4):
        a = int((desfase_s + i * beat) * sr)
        y[a:a + n] += bombo[: largo - a]
        clics[a:a + m] += rng.standard_normal(m)[: largo - a] * np.exp(-np.arange(m) / sr / 0.004)[: largo - a]
        h = int((desfase_s + (i + 0.5) * beat) * sr)
        hats[h:h + m] += rng.standard_normal(m)[: largo - h] * np.exp(-np.arange(m) / sr / 0.01)[: largo - h]
    if con_clic:
        y += 2.0 * banda(clics, 2500, 4500)
    if con_hats:
        y += 1.5 * banda(hats, 8000, 11000)
    return y.astype(np.float32)


def test_drum_grid_bombo_en_negras_y_hats_a_contratiempo(audios):
    bpm, compases = 120, 8
    audios["drums"] = bateria(bpm, compases)
    g = sw.drum_grid("drums", bpm, 0.0, compases * 2.0)
    assert g["steps_per_bar"] == 16 and g["bars"] == compases
    for k in ("kick", "snare", "hat"):
        assert len(g[k]) == compases and all(len(fila) == 16 for fila in g[k])
    for fila in g["kick"]:
        assert [i for i, v in enumerate(fila) if v] == [0, 4, 8, 12]
    for fila in g["hat"]:
        assert [i for i, v in enumerate(fila) if v] == [2, 6, 10, 14]
    assert sum(map(sum, g["snare"])) <= 2             # el cuerpo del bombo no se cuenta como caja


def test_drum_grid_corrige_el_ancla_con_el_bombo(audios):
    # Los golpes vienen 40 ms después del ancla declarada (un tema generado llega
    # con el ancla en NULL → 0): la rejilla igual los deja en el tiempo y los hats
    # en el contratiempo, no en el paso de al lado.
    bpm, compases = 120, 8
    audios["drums"] = bateria(bpm, compases, desfase_s=0.040)
    g = sw.drum_grid("drums", bpm, 0.0, compases * 2.0 + 0.04)
    assert g["bars"] == compases
    for fila in g["kick"]:
        assert [i for i, v in enumerate(fila) if v] == [0, 4, 8, 12]
    for fila in g["hat"][1:]:                         # el 1.er compás arranca con el borde del archivo
        assert [i for i, v in enumerate(fila) if v] == [2, 6, 10, 14]


def test_drum_grid_sin_clic_no_hay_bombo(audios):
    # Un grave sin transitorio es bajo colado, no bombo.
    bpm, compases = 120, 8
    audios["drums"] = bateria(bpm, compases, con_clic=False, con_hats=False)
    g = sw.drum_grid("drums", bpm, 0.0, compases * 2.0)
    assert sum(map(sum, g["kick"])) == 0


def test_drum_grid_audio_de_menos_de_un_segundo(audios):
    audios["drums"] = np.zeros(SR // 2, dtype=np.float32)
    assert sw.drum_grid("drums", 120, 0, 0.5) == {"steps_per_bar": 16, "bars": 0, "kick": [], "snare": [], "hat": []}


def test_drum_grid_tope_de_512_compases(audios):
    audios["drums"] = bateria(120, 2)
    g = sw.drum_grid("drums", 120, 0.0, 600 * 2.0)   # duración declarada absurda
    assert g["bars"] == 512 and len(g["kick"]) == 512


# ─────────────────────────────── perfil de sonido ───────────────────────────────
def test_sound_profile_mide_bombo_hats_bajo_y_otros(audios):
    bpm, compases = 120, 8
    audios["drums"] = bateria(bpm, compases)
    # Bajo de 55 Hz que se agacha 12 dB apenas pega cada bombo (sidechain).
    dur = compases * 2.0 + 1
    t = np.arange(int(dur * SR)) / SR
    gan = np.ones_like(t)
    for i in range(compases * 4):
        a, b = int(i * 0.5 * SR), int((i * 0.5 + 0.1) * SR)
        gan[a:b] = 10 ** (-12 / 20)
    audios["bass"] = (0.5 * np.sin(2 * np.pi * 55 * t) * gan).astype(np.float32)
    # «other»: golpes cortos que suben de golpe → stab
    otro = np.zeros(int(dur * SR), dtype=np.float32)
    for i in range(compases * 4):
        a = int((i * 0.5 + 0.25) * SR)
        otro[a:a + int(0.08 * SR)] = tono(880, 0.08)
    audios["other"] = otro
    audios["vocals"] = tono(300, dur, amp=0.2)
    rejilla = {"steps_per_bar": 16, "kick": [[1, 0, 0, 0] * 4] * compases, "hat": [[0, 0, 1, 0] * 4] * compases}
    p = sw.sound_profile({"drums": "drums", "bass": "bass", "other": "other", "vocals": "vocals"}, rejilla, bpm, 0)
    assert set(p) == {"kick", "hats", "bass", "other", "vocals"}
    assert p["kick"]["tune_hz"] == pytest.approx(55, abs=10)
    assert 20 < p["kick"]["decay_ms"] < 400
    assert p["hats"]["centroid_hz"] > 3000
    assert p["bass"]["sidechain_db"] == pytest.approx(-12, abs=3)
    assert p["bass"]["sidechain_muestras"] >= 8
    assert p["other"]["character"] == "stab" and p["other"]["attack_ms"] < 40
    assert p["vocals"]["centroid_hz"] == pytest.approx(300, rel=0.2)
    # todo redondeado a 3 decimales y en float (va directo a JSON)
    for rol in p.values():
        for v in rol.values():
            assert isinstance(v, (float, str, int))


def test_sound_profile_pad_que_sube_lento(audios):
    t = np.arange(int(20 * SR)) / SR
    audios["other"] = (0.3 * np.sin(2 * np.pi * 440 * t) * np.minimum(1, t / 4)).astype(np.float32)
    p = sw.sound_profile({"other": "other"}, {}, 120, 0)
    assert p["other"]["character"] == "pad"


def test_sound_profile_sin_rejilla_no_inventa_bateria(audios):
    audios["drums"] = bateria(120, 4)
    p = sw.sound_profile({"drums": "drums"}, {"steps_per_bar": 16, "kick": [], "hat": []}, 120, 0)
    assert p == {}


def test_sound_profile_error_devuelve_lo_que_alcanzo(audios):
    audios["bass"] = tono(55, 20)
    audios["other"] = RuntimeError("ffmpeg falló")
    p = sw.sound_profile({"bass": "bass", "other": "other"}, {}, 120, 0)
    assert "bass" in p and "other" not in p          # no revienta el trabajo entero
    assert "sidechain_db" not in p["bass"]            # sin bombos, no hay sidechain


# ─────────────────────────────── cola y apagado ───────────────────────────────
class Resp:
    def __init__(self, status=200, cuerpo=None):
        self.status_code, self._cuerpo, self.text = status, cuerpo or {}, ""
        self.ok = status < 400

    def json(self):
        return self._cuerpo

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


class Espia(list):
    """Lista de (url, cuerpo) enviados; `respuesta` es lo que contesta el servidor."""
    respuesta = Resp()


@pytest.fixture
def posts(monkeypatch):
    enviados = Espia()

    def post(url, headers=None, json=None, timeout=None):
        enviados.append((url, json))
        return enviados.respuesta

    monkeypatch.setattr(sw.requests, "post", post)
    return enviados


def test_claim_cola_vacia_y_con_trabajo(posts):
    posts.respuesta = Resp(204)
    assert sw.claim() is None
    posts.respuesta = Resp(200, {"job_id": "j1"})
    assert sw.claim() == {"job_id": "j1"}
    assert posts[-1][0].endswith("/stems-next")


def test_report_tapa_firmas_y_falla_si_el_servidor_rechaza(posts):
    sw.report("j1", False, error="403 en https://x.supabase.co/storage/v1/object/sign/a.mp3?token=eyJabc.def.ghi")
    cuerpo = posts[-1][1]
    assert cuerpo["ok"] is False and cuerpo["stems"] == {} and "eyJabc" not in cuerpo["error"]
    posts.respuesta = Resp(500)
    with pytest.raises(RuntimeError):
        sw.report("j1", True, stems={"drums": "x"})


def test_apagado_devuelve_la_separacion_en_curso(posts, monkeypatch):
    monkeypatch.setitem(sw.CURRENT_JOB, "id", "job-123")
    with pytest.raises(SystemExit) as e:
        sw._on_shutdown(15, None)
    assert e.value.code == 0
    assert posts == [(f"{sw.API}/stems-result", {"job_id": "job-123", "ok": False, "error": "requeue:shutdown"})]


def test_apagado_sin_trabajo_no_llama_a_nadie(posts, monkeypatch):
    monkeypatch.setitem(sw.CURRENT_JOB, "id", None)
    with pytest.raises(SystemExit):
        sw._on_shutdown(15, None)
    assert posts == []


def test_requeue_sin_red_no_revienta(monkeypatch):
    def caida(*a, **k):
        raise ConnectionError("sin red")
    monkeypatch.setattr(sw.requests, "post", caida)
    sw.requeue("job-123")                             # solo deja el aviso en el log


def test_lufs_of_sin_ffmpeg_devuelve_none(monkeypatch, tmp_path):
    def sin_ffmpeg(*a, **k):
        raise FileNotFoundError("ffmpeg")
    monkeypatch.setattr(sw.subprocess, "run", sin_ffmpeg)
    assert sw.lufs_of(Path(tmp_path / "x.wav")) is None


def test_lufs_of_lee_la_ultima_linea_integrada(monkeypatch, tmp_path):
    class Salida:
        stderr = ("[Parsed_ebur128_0] t: 1.0 M: -20.0 S: -21.0 I: -19.0 LUFS LRA: 0.0 LU\n"
                  "Summary:\n  Integrated loudness:\n    I:         -9.4 LUFS\n    Threshold: -19.4 LUFS\n")
    monkeypatch.setattr(sw.subprocess, "run", lambda *a, **k: Salida())
    assert sw.lufs_of(Path(tmp_path / "x.wav")) == -9.4
