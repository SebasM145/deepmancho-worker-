"""Respaldos del análisis: qué entrega analyze() cuando una pieza falla o no mide nada.
La regla de la casa es «nunca quedarse sin dato»: si se cae la rejilla, el BPM fino, el ancla o
los cues, el trabajo sigue y el tema sale con el mejor dato que quede (y siempre con cues).
Cada pieza cara se reemplaza por un doble para provocar la falla; no cambia código."""
import os
import sys

import numpy as np
import pytest
import soundfile as sf

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import worker  # noqa: E402

BPM = 120.0
DUR_S = 24.0  # 12 compases a 120


@pytest.fixture(scope="module")
def wav(tmp_path_factory):
    """Bombo en negras a 120 BPM con un bajo encima: suficiente para que analyze() no lo dé por mudo."""
    sr = 22050
    t = np.arange(int(DUR_S * sr)) / sr
    golpe = np.exp(-(t % (60.0 / BPM)) * 30) * np.sin(2 * np.pi * 55 * t)
    y = (0.7 * golpe + 0.1 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    ruta = tmp_path_factory.mktemp("respaldos") / "tema.wav"
    sf.write(str(ruta), y, sr)
    return str(ruta)


def cue(num, pos_ms, label=None):
    return {"number": num, "label": label or worker.CUE_DEF[num][0], "color": worker.CUE_DEF[num][1],
            "positionMs": int(pos_ms), "energy": 5, "confidence": 0.9}


class Dobles:
    """analyze() con las piezas caras simuladas. Cada atributo se puede cambiar por prueba:
    un valor es lo que devuelve la pieza; una excepción, lo que lanza."""

    def __init__(self, monkeypatch):
        self.rejilla = (BPM, 250)
        self.fino = (120.04, 4.2, 48)
        self.ancla = {"ancla_ms": 251.4, "residuo_ms": 1.1, "confianza": 0.93, "n_kicks": 48}
        self.cues = [cue(0, 0), cue(1, 8000), cue(7, 16000)]
        self.llamadas = {"detect_cues": [], "cuantizar": [], "compute_anchor": [], "refine_bpm": []}
        monkeypatch.setattr(worker, "detect_grid", lambda y, sr, seed_bpm=None: self._dar(self.rejilla))
        monkeypatch.setattr(worker, "refine_bpm", self._refine)
        monkeypatch.setattr(worker, "compute_anchor", self._anchor)
        monkeypatch.setattr(worker, "detect_cues", self._cues)
        monkeypatch.setattr(worker, "detect_vocal_segments", lambda *a: None)
        real = worker.cuantizar_cues

        def cuantizar(cues, bpm_grid, fb, dur_ms, mix=None):
            self.llamadas["cuantizar"].append({"bpm": bpm_grid, "fb": fb, "mix": mix})
            return real(cues, bpm_grid, fb, dur_ms, mix)
        monkeypatch.setattr(worker, "cuantizar_cues", cuantizar)

    @staticmethod
    def _dar(valor):
        if isinstance(valor, BaseException):
            raise valor
        return valor

    def _refine(self, y22, sr22, bpm_ref):
        self.llamadas["refine_bpm"].append(bpm_ref)
        return self._dar(self.fino)

    def _anchor(self, path, bpm):
        self.llamadas["compute_anchor"].append(bpm)
        return self._dar(self.ancla)

    def _cues(self, y, sr, bpm, fb):
        self.llamadas["detect_cues"].append({"bpm": bpm, "fb": fb})
        return self._dar(self.cues)


@pytest.fixture
def d(monkeypatch):
    return Dobles(monkeypatch)


def origenes(out):
    return {c.get("origen") for c in out["cue_points"]}


# ---------------------------------------------------------------- camino normal (referencia)

def test_camino_normal_con_los_dobles(wav, d):
    out = worker.analyze(wav)
    assert out["bpm"] == BPM and out["first_beat_offset_ms"] == 250
    assert out["bpm_precise"] == 120.04 and out["bpm_fine"] == pytest.approx(0.04)
    assert out["tempo_stability"] == "constante" and out["tempo_residual_ms"] == 4.2
    assert out["first_beat_detected_ms"] == 251 and out["grid_confidence"] == 0.93
    # Los cues se piden y se cuantizan con el BPM fino y el ancla definitiva, no con la rejilla vieja.
    assert d.llamadas["detect_cues"] == [{"bpm": 120.04, "fb": 251.4}]
    assert d.llamadas["cuantizar"] == [{"bpm": 120.04, "fb": 251.4, "mix": None}]
    assert [c["number"] for c in out["cue_points"]] == [0, 1, 7]
    assert isinstance(out["analysis_flags"], list)  # #206: siempre viaja, aunque venga vacía
    assert out["duration_seconds"] == 24
    assert "intro_djfriendly" not in out and "mixout_detected" not in out  # ENABLE_MIX_V7 apagado


# ---------------------------------------------------------------- la rejilla falla o da un BPM imposible

def con_rastreador(monkeypatch, tempo, cuadros):
    monkeypatch.setattr(worker.librosa.beat, "beat_track",
                        lambda **k: (tempo, np.asarray(cuadros, dtype=int)))


def test_si_detect_grid_revienta_usa_el_rastreador_de_antes(wav, d, monkeypatch):
    d.rejilla = RuntimeError("rejilla rota")
    con_rastreador(monkeypatch, np.array([119.84]), [10, 20, 30])  # librosa da el tempo como array
    out = worker.analyze(wav)
    assert out["bpm"] == 119.84
    assert out["first_beat_offset_ms"] == int(round(10 * 512 / worker.SR * 1000))
    # El resto del análisis sigue con ese BPM como referencia.
    assert d.llamadas["refine_bpm"] == [119.84]
    assert out["cue_points"]


@pytest.mark.parametrize("tempo", [np.array([300.0]), np.array([12.0]), np.array([]), None, 0.0])
def test_respaldo_con_tempo_imposible_deja_el_bpm_vacio(wav, d, monkeypatch, tempo):
    d.rejilla = RuntimeError("rejilla rota")
    con_rastreador(monkeypatch, tempo, [])
    out = worker.analyze(wav)
    assert out["bpm"] is None and out["first_beat_offset_ms"] is None
    # Sin BPM no hay pipeline fino ni rejilla: cues por tiempo (plan C), nunca vacío.
    assert d.llamadas["refine_bpm"] == []
    assert origenes(out) == {"tiempo"}
    assert [c["number"] for c in out["cue_points"]] == [0, 7]
    assert out["cue_points"][1]["positionMs"] == pytest.approx(DUR_S * 1000 * 0.85, abs=2)
    assert "tempo_stability" not in out and "analysis_flags" not in out


@pytest.mark.parametrize("bpm", [240.0, 300.0, 40.0, 12.5])
def test_bpm_de_la_rejilla_fuera_de_40_240_se_descarta_con_su_ancla(wav, d, bpm):
    d.rejilla = (bpm, 180)
    out = worker.analyze(wav)
    assert out["bpm"] is None and out["first_beat_offset_ms"] is None
    assert origenes(out) == {"tiempo"}


def test_con_semilla_el_pipeline_fino_corre_aunque_la_rejilla_no_mida(wav, d):
    # BPM bloqueado por el DJ: la semilla es la referencia aunque detect_grid no dé nada.
    d.rejilla = (300.0, 180)
    out = worker.analyze(wav, bpm_seed=120)
    assert out["bpm"] is None
    assert d.llamadas["refine_bpm"] == [120.0]
    assert out["bpm_precise"] == 120.04 and out["first_beat_detected_ms"] == 251
    assert [c["number"] for c in out["cue_points"]] == [0, 1, 7]


@pytest.mark.parametrize("semilla", [300, 20])
def test_semilla_imposible_no_entra_al_pipeline_fino(wav, d, semilla):
    out = worker.analyze(wav, bpm_seed=semilla)
    assert d.llamadas["refine_bpm"] == []
    assert out["bpm"] == BPM and "bpm_precise" not in out
    assert origenes(out) == {"grilla"}  # plan B con el BPM de la rejilla


# ---------------------------------------------------------------- el BPM fino no sale

def test_sin_bpm_fino_el_tempo_queda_desconocido_y_se_usa_el_de_referencia(wav, d):
    d.fino = (None, None, 7)
    out = worker.analyze(wav)
    assert out["tempo_stability"] == "desconocido"
    assert "bpm_precise" not in out and "bpm_fine" not in out and "tempo_residual_ms" not in out
    assert d.llamadas["compute_anchor"] == [BPM]
    assert d.llamadas["detect_cues"] == [{"bpm": BPM, "fb": 251.4}]
    assert out["first_beat_detected_ms"] == 251


def test_residuo_alto_marca_tempo_variable(wav, d):
    d.fino = (120.04, worker.TEMPO_RESID_MS + 0.1, 48)
    assert worker.analyze(wav)["tempo_stability"] == "variable"


# ---------------------------------------------------------------- el ancla falla

def test_si_el_ancla_falla_los_cues_van_sobre_la_rejilla_interna(wav, d, capsys):
    d.ancla = RuntimeError("CM2: muy pocos kicks detectados (3)")
    out = worker.analyze(wav)
    assert "first_beat_detected_ms" not in out and "grid_confidence" not in out
    assert out["first_beat_offset_ms"] == 250
    assert d.llamadas["detect_cues"] == [{"bpm": 120.04, "fb": 250.0}]
    assert d.llamadas["cuantizar"] == [{"bpm": 120.04, "fb": 250.0, "mix": None}]
    assert out["bpm_precise"] == 120.04 and out["cue_points"]
    assert "fallback a la rejilla interna" in capsys.readouterr().out


def test_ancla_caida_y_rejilla_sin_primer_beat_usa_cero(wav, d):
    d.rejilla = (BPM, None)
    d.ancla = RuntimeError("CM2: audio vacío")
    worker.analyze(wav)
    assert d.llamadas["detect_cues"] == [{"bpm": 120.04, "fb": 0.0}]


# ---------------------------------------------------------------- los cues no salen

@pytest.mark.parametrize("vacio", [None, []])
def test_sin_estructura_medible_entra_el_plan_b_de_rejilla(wav, d, vacio, capsys):
    d.cues = vacio
    out = worker.analyze(wav)
    assert origenes(out) == {"grilla"}
    assert out["cue_points"][0]["number"] == 0
    # También el plan B queda cuantizado a la rejilla definitiva.
    assert d.llamadas["cuantizar"] == [{"bpm": 120.04, "fb": 251.4, "mix": None}]
    assert "plan B de cues (rejilla)" in capsys.readouterr().out


def test_si_el_pipeline_fino_revienta_el_trabajo_no_se_bloquea(wav, d, capsys):
    d.fino = ValueError("refine_bpm roto")
    out = worker.analyze(wav)
    assert "v7 falló (no bloquea el job)" in capsys.readouterr().out
    # Lo básico sigue ahí y los cues salen del plan B con el BPM de la rejilla.
    assert out["bpm"] == BPM and out["first_beat_offset_ms"] == 250
    assert out["waveform_peaks"] and out["energy"] is not None
    assert "bpm_precise" not in out and "analysis_flags" not in out
    assert origenes(out) == {"grilla"}
    assert out["duration_seconds"] == 24


def test_si_revienta_despues_de_los_cues_se_conservan(wav, d, monkeypatch):
    def roto(*a):
        raise RuntimeError("sanity_check roto")
    monkeypatch.setattr(worker, "sanity_check", roto)
    out = worker.analyze(wav)
    assert [c["number"] for c in out["cue_points"]] == [0, 1, 7]
    assert origenes(out) == {None}  # son los del detector, no los de respaldo
    assert "analysis_confidence" not in out and "analysis_flags" not in out


def test_la_huella_de_rasgos_no_bloquea(wav, d, monkeypatch, capsys):
    def rota(*a, **k):
        raise RuntimeError("huella rota")
    monkeypatch.setattr(worker.parecido, "huella", rota)
    out = worker.analyze(wav)
    assert "rasgos" not in out and out["bpm_precise"] == 120.04
    assert "rasgos: fallo (no bloquea)" in capsys.readouterr().out


# ---------------------------------------------------------------- MIX-IN / MIX-OUT v7 (apagado por defecto)

def con_mix_v7(monkeypatch, d, mi, mo):
    monkeypatch.setattr(worker, "ENABLE_MIX_V7", True)
    monkeypatch.setattr(worker, "detect_mix_in", lambda y, sr, bpm, fb: (mi, True))
    monkeypatch.setattr(worker, "detect_mix_out", lambda y, sr, bpm, fb: (mo, False))
    d.cues = [cue(0, 0), cue(1, 4000, "MIX-IN"), cue(7, 16000, "MIX-OUT")]


def test_mix_v7_encendido_guarda_las_dos_marcas_y_mueve_los_cues(wav, d, monkeypatch):
    con_mix_v7(monkeypatch, d, 2251, 18251)
    out = worker.analyze(wav)
    assert out["intro_djfriendly"] is True and out["mixout_detected"] is False
    assert d.llamadas["cuantizar"][0]["mix"] == (2251, 18251)
    por_label = {c["label"]: c["positionMs"] for c in out["cue_points"]}
    compas = 4 * 60000 / 120.04
    assert por_label["MIX-IN"] == pytest.approx(251.4 + compas, abs=1)       # 2251 -> compás 1
    assert por_label["MIX-OUT"] == pytest.approx(251.4 + 9 * compas, abs=1)  # 18251 -> compás 9


@pytest.mark.parametrize("mi,mo", [(None, 18000), (2000, None), (18000, 2000), (5000, 5000)])
def test_mix_v7_sin_par_valido_deja_los_cues_donde_estaban(wav, d, monkeypatch, mi, mo):
    con_mix_v7(monkeypatch, d, mi, mo)
    out = worker.analyze(wav)
    assert d.llamadas["cuantizar"][0]["mix"] is None
    por_label = {c["label"]: c["positionMs"] for c in out["cue_points"]}
    compas = 4 * 60000 / 120.04
    assert por_label["MIX-IN"] == pytest.approx(251.4 + 2 * compas, abs=1)   # 4000 -> compás 2
    assert por_label["MIX-OUT"] == pytest.approx(251.4 + 8 * compas, abs=1)  # 16000 -> compás 8


# ---------------------------------------------------------------- piezas sueltas

def test_clasificar_tempo():
    assert worker.clasificar_tempo(None) == "desconocido"
    assert worker.clasificar_tempo(0) == "constante"
    assert worker.clasificar_tempo(worker.TEMPO_RESID_MS) == "constante"
    assert worker.clasificar_tempo(worker.TEMPO_RESID_MS + 0.1) == "variable"


@pytest.mark.parametrize("dur_ms", [None, 0, 3999])
def test_cues_por_tiempo_en_audio_muy_corto_solo_pone_el_inicio(dur_ms):
    cues = worker.cues_por_tiempo(dur_ms)
    assert [(c["number"], c["positionMs"], c["origen"]) for c in cues] == [(0, 0, "tiempo")]


def test_cues_por_tiempo_pone_la_salida_al_85_por_ciento():
    cues = worker.cues_por_tiempo(200000)
    assert [(c["number"], c["positionMs"]) for c in cues] == [(0, 0), (7, 170000)]
    assert cues[0]["confidence"] == 1.0 and cues[1]["confidence"] == 0.2


@pytest.mark.parametrize("bpm", [None, 0, 40, 240, 300])
def test_cues_respaldo_sin_bpm_util_no_inventa_rejilla(bpm):
    assert worker.cues_respaldo(np.ones(11025 * 30, dtype=np.float32), 11025, bpm, 0) is None


@pytest.mark.parametrize("ancla", [-50, 999999])
def test_cues_respaldo_con_ancla_fuera_del_audio_parte_de_cero(ancla):
    y = np.ones(11025 * 60, dtype=np.float32) * 0.1
    cues = worker.cues_respaldo(y, 11025, 120, ancla)
    assert cues[0]["number"] == 0 and cues[0]["positionMs"] == 0
    compas = 2000.0
    assert all(c["positionMs"] % compas == 0 for c in cues)


def test_cues_respaldo_si_la_medida_revienta_queda_el_cue_de_inicio(monkeypatch, capsys):
    def roto(*a, **k):
        raise FloatingPointError("energías rotas")
    monkeypatch.setattr(worker, "_bar_band_energies", roto)
    cues = worker.cues_respaldo(np.ones(11025 * 60, dtype=np.float32), 11025, 120, 300)
    assert [(c["number"], c["positionMs"], c["origen"]) for c in cues] == [(0, 0, "grilla")]
    assert "cues_respaldo fallo" in capsys.readouterr().out


def test_ajuste_por_bombos_con_pocos_golpes_no_mide(monkeypatch):
    monkeypatch.setattr(worker, "ataques_de_bombo", lambda y, sr, p: (np.arange(10) * 0.5, np.ones(10)))
    assert worker.ajuste_por_bombos(np.zeros(10), 22050, 120) == (None, None, 10)


def test_ajuste_por_bombos_descarta_lo_que_cae_lejos_de_la_rejilla(monkeypatch):
    # 20 golpes en la rejilla de 120 y 20 a un cuarto de beat: solo los primeros sirven, y 20 < 32.
    t = np.concatenate([np.arange(20) * 0.5, np.arange(20) * 0.5 + 0.125])
    monkeypatch.setattr(worker, "ataques_de_bombo", lambda y, sr, p: (np.sort(t), np.ones(40)))
    monkeypatch.setattr(worker, "fase_de_bombos", lambda t, pesos, b: (1.0 if abs(b - 120) < 1e-6 else 0.0, 0.0))
    bpm, resid, n = worker.ajuste_por_bombos(np.zeros(10), 22050, 120)
    assert (bpm, resid) == (None, None) and n == 20


def test_ajuste_por_bombos_mide_una_rejilla_limpia(monkeypatch):
    t = np.arange(64) * (60.0 / 120.3)
    monkeypatch.setattr(worker, "ataques_de_bombo", lambda y, sr, p: (t, np.ones(64)))
    bpm, resid, n = worker.ajuste_por_bombos(np.zeros(10), 22050, 120)
    assert bpm == pytest.approx(120.3, abs=0.01) and resid < 1.0 and n == 64


def rastreador_con(monkeypatch, tiempos):
    monkeypatch.setattr(worker.librosa.onset, "onset_strength", lambda **k: np.ones(64))
    monkeypatch.setattr(worker, "tempo_por_bloques", lambda env, sr, hop, bpm: np.array([bpm]))
    monkeypatch.setattr(worker.librosa.beat, "beat_track", lambda **k: (120.0, np.arange(len(tiempos))))
    monkeypatch.setattr(worker.librosa, "frames_to_time", lambda beats, **k: np.asarray(tiempos, dtype=float))


def test_rastreador_con_pocos_beats_no_mide(monkeypatch):
    rastreador_con(monkeypatch, np.arange(31) * 0.5)
    assert worker._ajuste_por_rastreador(np.zeros(10), 22050, 120) == (None, None, 31)


def test_rastreador_descarta_beats_lejos_de_la_rejilla(monkeypatch):
    # 40 beats, pero la mitad cae a contratiempo: quedan 20 y no alcanza para ajustar.
    t = np.arange(40) * 0.5
    t[1::2] += 0.2
    rastreador_con(monkeypatch, t)
    assert worker._ajuste_por_rastreador(np.zeros(10), 22050, 120) == (None, None, 20)


def test_rastreador_mide_el_decimal_del_tempo(monkeypatch):
    rastreador_con(monkeypatch, 0.3 + np.arange(64) * (60.0 / 120.2))
    bpm, resid, n = worker._ajuste_por_rastreador(np.zeros(10), 22050, 120)
    assert bpm == pytest.approx(120.2, abs=0.001) and resid < 0.5 and n == 64


def test_rastreador_que_revienta_no_tumba_el_analisis(monkeypatch):
    def roto(**k):
        raise RuntimeError("librosa roto")
    monkeypatch.setattr(worker.librosa.onset, "onset_strength", roto)
    assert worker._ajuste_por_rastreador(np.zeros(10), 22050, 120) == (None, None, 0)


def test_tonalidad_y_energia_que_fallan_devuelven_vacio(monkeypatch):
    def roto(**k):
        raise RuntimeError("chroma roto")
    monkeypatch.setattr(worker.librosa.feature, "chroma_stft", roto)
    assert worker.detect_key(np.zeros(100), 11025) == (None, None)
    assert worker.compute_energy(np.ones(4), None) is None  # bandas ausentes: no hay .get


# ---------------------------------------------------------------- compute_anchor rechaza lo que no puede medir

def con_audio(monkeypatch, y, sr=None):
    sr = sr or worker.ANCHOR_SR
    monkeypatch.setattr(worker.librosa, "load", lambda path, **k: (np.asarray(y, dtype=np.float32), sr))


def test_ancla_audio_vacio(monkeypatch):
    con_audio(monkeypatch, [])
    with pytest.raises(RuntimeError, match="CM2: audio vacío"):
        worker.compute_anchor("x.mp3", 120)


def test_ancla_silencio_no_tiene_graves(monkeypatch):
    con_audio(monkeypatch, np.zeros(worker.ANCHOR_SR * 4))
    with pytest.raises(RuntimeError, match="CM2: sin energía de graves"):
        worker.compute_anchor("x.mp3", 120)


def test_ancla_con_menos_de_24_bombos_no_se_mide(monkeypatch):
    sr = worker.ANCHOR_SR
    t = np.arange(int(5 * sr)) / sr  # 5 s a 120 BPM: 10 bombos
    y = np.exp(-(t % 0.5) * 30) * np.sin(2 * np.pi * 60 * t)
    con_audio(monkeypatch, y)
    with pytest.raises(RuntimeError, match=r"CM2: muy pocos kicks detectados \(\d+\)"):
        worker.compute_anchor("x.mp3", 120)
