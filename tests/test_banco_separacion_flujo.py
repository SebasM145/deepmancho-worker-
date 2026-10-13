"""Relevo de noche 13-oct: el flujo completo del banco A/B de separación.

`test_banco_separacion.py` fija las medidas una por una. Aquí va lo que las junta y que
no tenía ninguna prueba: la separación en un proceso aparte (`separar_medido` y lo que
corre adentro), el tempo estimado cuando no hay tempos.csv, `loops_aprobados`,
`notas_bajo`, `medir_tema` (una fila por tema × motor) y `main` (resultados.csv,
clave_ciega.json y resumen.txt).

Sin demucs, sin torch, sin ffmpeg y sin Basic Pitch: los motores son dobles que
entregan pistas sintéticas de numpy. El banco no toca producción y estas pruebas tampoco.
Fijan el comportamiento actual: no cambian código.
"""
import csv
import json
import os
import random
import sys
import types
from pathlib import Path

import numpy as np
import numpy.testing  # noqa: F401  (se carga ya: al importarse llama a subprocess.run, que aquí es un espía)
import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import banco_separacion as bs  # noqa: E402
import stems_worker as sw  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_banco_separacion import bombos_en, ruido, tono  # noqa: E402

SR = bs.SR


# ───────────────────────────── separación en un proceso aparte ─────────────────────────────
def proceso(stdout="", stderr="", returncode=0):
    return types.SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def test_separar_medido_lanza_el_banco_como_subproceso_y_lee_el_resultado(monkeypatch, tmp_path):
    llamadas = []
    info = {"engine": "demucs", "ram_pico_mb": 812.5, "pistas": {"drums": "/x/drums.mp3", "bass": "/x/bass.mp3"}}

    def run(cmd, **kw):
        llamadas.append((cmd, kw))
        return proceso("demucs: -n htdemucs_6s\n@@RESULTADO@@" + json.dumps(info) + "\nlisto\n")

    monkeypatch.setattr(bs.subprocess, "run", run)
    r = bs.separar_medido("demucs", tmp_path / "tema.mp3", tmp_path / "out")
    cmd, kw = llamadas[0]
    # Otro proceso de Python con este mismo archivo: así la RAM pico es la de esa separación sola.
    assert cmd == [sys.executable, bs.__file__, "--_separar", "demucs", str(tmp_path / "tema.mp3"), str(tmp_path / "out")]
    assert kw == {"capture_output": True, "text": True}
    assert r["pistas"] == {"drums": Path("/x/drums.mp3"), "bass": Path("/x/bass.mp3")}
    assert r["engine"] == "demucs" and r["ram_pico_mb"] == 812.5
    assert isinstance(r["segundos"], float) and r["segundos"] >= 0


@pytest.mark.parametrize("stdout,stderr,esperado", [
    ("cargando…", "Killed", "Killed"),
    ("RuntimeError: sin pesos", "", "sin pesos"),      # sin stderr se muestra lo que imprimió
])
def test_separar_medido_sin_resultado_dice_que_motor_fallo(monkeypatch, tmp_path, stdout, stderr, esperado):
    monkeypatch.setattr(bs.subprocess, "run", lambda cmd, **kw: proceso(stdout, stderr, 137))
    with pytest.raises(RuntimeError) as e:
        bs.separar_medido("hibrido", tmp_path / "t.mp3", tmp_path / "out")
    assert "hibrido falló (137)" in str(e.value) and esperado in str(e.value)


def test_separar_medido_solo_muestra_el_final_de_un_error_largo(monkeypatch, tmp_path):
    monkeypatch.setattr(bs.subprocess, "run", lambda cmd, **kw: proceso("", "INICIO" + "x" * 3000 + "FIN", 1))
    with pytest.raises(RuntimeError) as e:
        bs.separar_medido("demucs", tmp_path / "t.mp3", tmp_path / "out")
    assert "FIN" in str(e.value) and "INICIO" not in str(e.value)


def resultado_impreso(capsys):
    lineas = [x for x in capsys.readouterr().out.splitlines() if x.startswith("@@RESULTADO@@")]
    assert len(lineas) == 1
    return json.loads(lineas[0][len("@@RESULTADO@@"):])


def demucs_que_deja(nombres):
    def run_demucs(src, tmp, model):
        base = tmp / model / src.stem
        base.mkdir(parents=True)
        salida = {}
        for n in nombres:
            (base / f"{n}.mp3").write_bytes(n.encode())
            salida[n] = base / f"{n}.mp3"
        return salida
    return run_demucs


def test_adentro_del_subproceso_demucs_deja_las_pistas_en_la_carpeta(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sw, "run_demucs", demucs_que_deja(["drums", "bass"]))
    out = tmp_path / "separado" / "demucs" / "tema"
    bs._separar_en_este_proceso("demucs", tmp_path / "tema.mp3", out)
    assert sorted(p.name for p in out.iterdir()) == ["bass.mp3", "drums.mp3"]  # sin la carpeta temporal
    assert (out / "drums.mp3").read_bytes() == b"drums"
    info = resultado_impreso(capsys)
    assert info["engine"] == "demucs" and info["model"] == sw.modelo_demucs(None)
    assert info["pistas"] == {"drums": str(out / "drums.mp3"), "bass": str(out / "bass.mp3")}
    assert info["ram_pico_mb"] > 0


def test_adentro_del_subproceso_el_hibrido_va_sin_respaldo(monkeypatch, tmp_path, capsys):
    out = tmp_path / "out"
    monkeypatch.setattr(sw, "separar_hibrido", lambda src, o: {"vocals": o / "vocals.mp3"})
    monkeypatch.setattr(sw, "run_demucs", lambda *a: pytest.fail("el banco no debe caer en demucs"))
    bs._separar_en_este_proceso("hibrido", tmp_path / "tema.mp3", out)
    info = resultado_impreso(capsys)
    assert info["engine"] == "hibrido" and info["pistas"] == {"vocals": str(out / "vocals.mp3")}
    assert out.is_dir()

    def falla(src, o):
        raise RuntimeError("sin pesos")

    monkeypatch.setattr(sw, "separar_hibrido", falla)
    with pytest.raises(RuntimeError, match="sin pesos"):
        bs._separar_en_este_proceso("hibrido", tmp_path / "tema.mp3", out)
    assert "@@RESULTADO@@" not in capsys.readouterr().out  # si falla, el banco lo dice: no hay resultado


@pytest.mark.parametrize("plataforma,esperado", [("darwin", 2.0), ("linux", 2048.0)])
def test_la_ram_pico_sale_en_mb_en_mac_y_en_linux(monkeypatch, tmp_path, capsys, plataforma, esperado):
    """macOS da bytes y Linux KB; se toma el mayor entre el proceso y sus hijos (demucs es un hijo)."""
    monkeypatch.setattr(sw, "separar_hibrido", lambda src, o: {})
    usos = {bs.resource.RUSAGE_SELF: 500_000, bs.resource.RUSAGE_CHILDREN: 2_000_000}
    monkeypatch.setattr(bs.resource, "getrusage", lambda quien: types.SimpleNamespace(ru_maxrss=usos[quien]))
    monkeypatch.setattr(bs.sys, "platform", plataforma)
    bs._separar_en_este_proceso("hibrido", tmp_path / "t.mp3", tmp_path / "out")
    monkeypatch.undo()
    assert resultado_impreso(capsys)["ram_pico_mb"] == esperado


def test_ida_y_vuelta_lo_que_imprime_el_subproceso_lo_entiende_el_banco(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sw, "run_demucs", demucs_que_deja(["vocals", "drums"]))
    out = tmp_path / "out"
    bs._separar_en_este_proceso("demucs", tmp_path / "tema.mp3", out)
    impreso = capsys.readouterr().out
    monkeypatch.setattr(bs.subprocess, "run", lambda cmd, **kw: proceso(impreso))
    r = bs.separar_medido("demucs", tmp_path / "tema.mp3", out)
    assert r["pistas"] == {"vocals": out / "vocals.mp3", "drums": out / "drums.mp3"}
    assert all(p.is_file() for p in r["pistas"].values())


# ───────────────────────────── loops y notas del bajo ─────────────────────────────
def test_loops_aprobados_recorre_las_ventanas_de_8_compases(monkeypatch):
    pedidos = []

    def controlar_loop(y, sr, bpm, fase_ms, desde, pista):
        pedidos.append((pista, desde, bpm, fase_ms))
        if pista == "drums":
            return True, {}
        if desde == 1:
            return False, {"motivo": "poca energía (−48 dB)"}
        return False, {}

    monkeypatch.setattr(sw, "controlar_loop", controlar_loop)
    sr, bpm = 100, 120.0                      # compás de 2 s
    audio = {"drums": (np.zeros(40 * sr), sr),   # 20 compases → ventanas en el compás 1 y el 9
             "bass": (np.zeros(40 * sr), sr),
             "piano": (np.zeros(10 * sr), sr)}   # 5 compases: no cabe ninguna ventana
    por_pista, motivos = bs.loops_aprobados(audio, bpm, fase_ms=12.5)
    assert por_pista == {"drums": (2, 2), "bass": (0, 2)}
    assert [p[:2] for p in pedidos] == [("drums", 1), ("drums", 9), ("bass", 1), ("bass", 9)]
    assert all(p[2:] == (120.0, 12.5) for p in pedidos)
    # El motivo se cuenta sin el detalle entre paréntesis; sin motivo queda «?».
    assert motivos == {"poca energía": 1, "?": 1}


def test_loops_aprobados_con_exactamente_8_compases_hay_una_ventana(monkeypatch):
    monkeypatch.setattr(sw, "controlar_loop", lambda *a: (True, {}))
    sr = 100
    assert bs.loops_aprobados({"drums": (np.zeros(16 * sr), sr)}, 120.0, 0.0) == ({"drums": (1, 1)}, {})


def test_notas_bajo_calla_a_basic_pitch(monkeypatch, capsys):
    pedidos = []

    def midi_notes(path, to_beat, lo, hi):
        print("Predicting MIDI for bass.mp3...")
        pedidos.append((path, to_beat, lo, hi))
        return [{"n": 33, "b": 0.0}]

    monkeypatch.setattr(sw, "midi_notes", midi_notes)
    assert bs.notas_bajo(Path("bass.mp3"), "a_tiempos") == [{"n": 33, "b": 0.0}]
    assert pedidos == [(Path("bass.mp3"), "a_tiempos", 24, 60)]  # solo el registro del bajo
    assert capsys.readouterr().out == ""


# ───────────────────────────── tempo estimado (sin tempos.csv) ─────────────────────────────
@pytest.fixture
def librosa_dice(monkeypatch):
    """`librosa.beat.beat_track` responde el tempo que la prueba le pida."""
    import librosa
    dicho = {"bpm": 120.0}
    monkeypatch.setattr(librosa.beat, "beat_track", lambda y, sr: (np.array([dicho["bpm"]]), np.array([])))
    return dicho


def tema_con_bombos(monkeypatch, bpm, ancla_s=0.1, dur_s=30.0):
    y = bombos_en(np.arange(ancla_s, dur_s - 0.5, 60.0 / bpm), dur_s).astype(np.float32)
    monkeypatch.setattr(sw, "to_wav_mono", lambda path, sr=SR: (y, sr))


@pytest.mark.parametrize("librosa_bpm", [123.0, 61.5, 246.0, 127.0])
def test_tempo_estimado_se_afina_con_los_bombos(monkeypatch, librosa_dice, librosa_bpm):
    """librosa se equivoca por varios BPM, o da la mitad o el doble: mandan los bombos del tema."""
    tema_con_bombos(monkeypatch, 124.0)
    librosa_dice["bpm"] = librosa_bpm
    bpm, ancla, origen = bs.tempo_y_ancla(Path("tema.mp3"), {})
    assert bpm == 124.0
    assert origen.startswith("estimado")
    beat_ms = 60000.0 / 124
    assert abs((ancla - 100.0 + beat_ms / 2) % beat_ms - beat_ms / 2) < 15.0  # el ancla cae en los bombos


def test_tempo_estimado_conserva_un_tempo_que_no_es_entero(monkeypatch, librosa_dice):
    """Con un minuto de bombos, medio BPM ya se distingue del entero vecino (en un recorte
    de 30 s todavía no: ahí el banco prefiere el entero)."""
    tema_con_bombos(monkeypatch, 124.5, dur_s=60.0)
    librosa_dice["bpm"] = 125.0
    bpm, _, _ = bs.tempo_y_ancla(Path("tema.mp3"), {})
    assert bpm == pytest.approx(124.5, abs=0.05)


def test_tempo_estimado_sin_bombos_queda_con_lo_de_librosa_y_ancla_cero(monkeypatch, librosa_dice):
    monkeypatch.setattr(sw, "to_wav_mono", lambda path, sr=SR: (np.zeros(10 * SR, dtype=np.float32), sr))
    librosa_dice["bpm"] = 65.0
    bpm, ancla, origen = bs.tempo_y_ancla(Path("tema.mp3"), {})
    assert (bpm, ancla) == (130.0, 0.0)      # la mitad se dobla al rango de baile; sin bombos no hay fase
    assert origen.startswith("estimado")


def test_tempo_y_ancla_no_estima_si_el_tema_esta_en_tempos_csv(monkeypatch):
    monkeypatch.setattr(sw, "to_wav_mono", lambda *a, **k: pytest.fail("no debía abrir el audio"))
    assert bs.tempo_y_ancla(Path("/lejos/tema.mp3"), {"tema.mp3": (128.0, 12.0)}) == (128.0, 12.0, "tempos.csv")


# ───────────────────────────── medir_tema ─────────────────────────────
BPM, ANCLA_MS, DUR = 120.0, 100.0, 24.0      # compás de 2 s → 12 compases → una ventana de loops por pista
TARDE = 551                                   # ≈ 25 ms a 22 050 Hz: el atraso de los mp3 de demucs


def atrasar(y, n):
    return np.concatenate([np.zeros(n, dtype=y.dtype), y[:-n]])


@pytest.fixture
def banco(monkeypatch, tmp_path):
    """Un tema sintético y dos motores de mentira.
    - «demucs» separa perfecto pero entrega las pistas 25 ms tarde.
    - «hibrido» entrega a tiempo pero deja el 30 % de la voz en «other»."""
    n = int(DUR * SR)
    encendida = (np.floor(np.arange(n) / SR * 2) % 2 == 0)             # la voz entra y sale cada medio segundo
    voz = (bs._banda(ruido(DUR, 5), SR, 1000, 4000) * encendida * 0.5).astype(np.float32)
    bat = bombos_en(np.arange(ANCLA_MS / 1000, DUR - 0.5, 60.0 / BPM), DUR).astype(np.float32)
    bajo = tono(55, DUR, amp=0.3).astype(np.float32)
    otro = tono(440, DUR, amp=0.1).astype(np.float32)
    b = types.SimpleNamespace(voz=voz, bat=bat, bajo=bajo, otro=otro, mezcla=voz + bat + bajo + otro)
    b.src = tmp_path / "tema.mp3"
    b.salida = tmp_path / "banco"
    b.audio = {str(b.src): b.mezcla}
    b.motores = {
        "demucs": {"vocals": atrasar(voz, TARDE), "drums": atrasar(bat, TARDE), "bass": atrasar(bajo, TARDE),
                   "other": atrasar(otro, TARDE)},
        "hibrido": {"vocals": 0.7 * voz, "drums": bat, "bass": bajo, "other": otro + 0.3 * voz},
    }
    b.separados, b.loops, b.calidad = [], [], []

    def separar_medido(motor, src, out):
        assert not out.exists()                      # la carpeta de una corrida anterior se borra antes
        b.separados.append((motor, src, out))
        pistas = {}
        for nombre, y in b.motores[motor].items():
            pistas[nombre] = out / f"{nombre}.mp3"
            b.audio[str(pistas[nombre])] = y.astype(np.float32)
        return {"engine_version": f"{motor}-v1", "segundos": 12.3, "ram_pico_mb": 800.0, "pistas": pistas}

    def controlar_loop(y, sr, bpm, fase_ms, desde, pista):
        b.loops.append((pista, desde, bpm, fase_ms))
        return (True, {}) if pista != "other" else (False, {"motivo": "poca energía (−50 dB)"})

    def calidad_pistas(stems):
        b.calidad.append(dict(stems))
        return {"vocals": {"estado": "limpia", "correlacion": 0.1}, "drums": {"estado": "limpia", "correlacion": 0.2},
                "other": {"estado": "mezclada", "correlacion": 0.8}}

    def notas_bajo(path, to_beat):
        assert to_beat(ANCLA_MS / 1000 + 0.5) == pytest.approx(1.0)   # la rejilla del tema: 1 tiempo = 0,5 s
        if "refs" in str(path):
            return [{"n": 33, "b": 0.1}, {"n": 40, "b": 2.0}]
        return [{"n": 33, "b": 0.0}, {"n": 36, "b": 1.0}]

    monkeypatch.setattr(bs, "separar_medido", separar_medido)
    monkeypatch.setattr(bs, "notas_bajo", notas_bajo)
    monkeypatch.setattr(sw, "controlar_loop", controlar_loop)
    monkeypatch.setattr(sw, "calidad_pistas", calidad_pistas)
    monkeypatch.setattr(sw, "to_wav_mono", lambda path, sr=SR: (b.audio[str(path)], sr))
    monkeypatch.setattr(sw, "BPM_GLOBAL", list(sw.BPM_GLOBAL))
    b.tempos = {"tema.mp3": (BPM, ANCLA_MS)}
    return b


def referencias(b, tmp_path, pistas=("vocals", "bass")):
    carpeta = tmp_path / "refs" / "tema"
    carpeta.mkdir(parents=True)
    reales = {"vocals": b.voz, "bass": b.bajo}
    for p in pistas:
        (carpeta / f"{p}.wav").write_bytes(b"")
        b.audio[str(carpeta / f"{p}.wav")] = reales[p]
    (carpeta / "notas.txt").write_text("no es una pista")
    return tmp_path / "refs"


def test_medir_tema_da_una_fila_por_motor(banco):
    (banco.salida / "separado" / "demucs" / "tema").mkdir(parents=True)
    (banco.salida / "separado" / "demucs" / "tema" / "viejo.mp3").write_bytes(b"de otra corrida")
    filas, runs = bs.medir_tema(banco.src, banco.salida, ["demucs", "hibrido"], banco.tempos, None)
    assert [f["motor"] for f in filas] == ["demucs", "hibrido"] and list(runs) == ["demucs", "hibrido"]
    assert [s[2] for s in banco.separados] == [banco.salida / "separado" / m / "tema" for m in ("demucs", "hibrido")]
    for f in filas:
        assert (f["tema"], f["bpm"], f["ancla_ms"]) == ("tema", 120.0, 100.0)
        assert (f["segundos"], f["ram_pico_mb"], f["engine_version"]) == (12.3, 800.0, f"{f['motor']}-v1")
    assert sw.BPM_GLOBAL[0] == 120.0   # las medidas del worker leen el tempo de ahí


def test_medir_tema_calidad_con_la_misma_funcion_que_produccion(banco):
    filas, runs = bs.medir_tema(banco.src, banco.salida, ["demucs"], banco.tempos, None)
    f = filas[0]
    assert banco.calidad == [runs["demucs"]["pistas"]]
    assert (f["calidad_vocals"], f["corr_vocals"]) == ("limpia", 0.1)
    assert (f["calidad_other"], f["corr_other"]) == ("mezclada", 0.8)
    assert (f["calidad_bass"], f["corr_bass"]) == (None, None)       # pista sin medida: vacía, no inventada
    assert f["pistas_limpias"] == 2


def test_medir_tema_mide_el_atraso_de_las_pistas_contra_la_mezcla(banco):
    filas, _ = bs.medir_tema(banco.src, banco.salida, ["demucs", "hibrido"], banco.tempos, None)
    demucs, hibrido = filas
    assert demucs["desfase_pistas_ms"] == pytest.approx(25.0, abs=0.2)
    assert hibrido["desfase_pistas_ms"] == 0.0
    # El bombo «tal cual se entrega» carga con esos 25 ms; descontándolos, los dos motores quedan iguales.
    assert demucs["bombo_desvio_signo_ms"] - hibrido["bombo_desvio_signo_ms"] == pytest.approx(25.0, abs=0.5)
    assert demucs["bombo_desvio_ms"] > 20.0 > 5.0 > hibrido["bombo_desvio_ms"]
    assert demucs["bombo_desvio_alineado_ms"] == pytest.approx(hibrido["bombo_desvio_ms"], abs=0.5)
    assert hibrido["bombo_desvio_alineado_ms"] == hibrido["bombo_desvio_ms"]
    assert demucs["bombos"] > 16 and hibrido["bombos"] > 16
    assert abs(hibrido["deriva_ms_8c"]) < 1.0


def test_medir_tema_los_loops_usan_la_fase_de_la_bateria_de_cada_motor(banco):
    filas, _ = bs.medir_tema(banco.src, banco.salida, ["demucs", "hibrido"], banco.tempos, None)
    demucs, hibrido = filas
    assert demucs["fase_bateria_ms"] - hibrido["fase_bateria_ms"] == pytest.approx(25.0, abs=1.0)
    fases = {}
    for (pista, desde, bpm, fase), motor in zip(banco.loops, ["demucs"] * 4 + ["hibrido"] * 4):
        assert (desde, bpm) == (1, 120.0)
        fases.setdefault(motor, set()).add(round(fase, 2))
    assert fases == {"demucs": {demucs["fase_bateria_ms"]}, "hibrido": {hibrido["fase_bateria_ms"]}}
    for f in filas:
        assert (f["loops_aprobados"], f["loops_ventanas"], f["loops_pct"]) == (3, 4, 75.0)
        assert (f["loops_vocals"], f["loops_other"]) == ("1/1", "0/1")
        assert json.loads(f["loops_rechazos"]) == {"poca energía": 1}


def test_medir_tema_la_fuga_de_voz_delata_al_motor_que_la_deja_pasar(banco):
    filas, _ = bs.medir_tema(banco.src, banco.salida, ["demucs", "hibrido"], banco.tempos, None)
    demucs, hibrido = filas
    assert hibrido["fuga_voz_corr"] > 0.8 > demucs["fuga_voz_corr"]
    # Sin pistas de referencia no hay medida absoluta: las columnas no se inventan.
    for f in filas:
        assert "fuga_voz_db" not in f and "bajo_f1_ref" not in f
        assert (f["bajo_notas"], f["bajo_rango"]) == (2, "33-36")


def test_medir_tema_con_referencias_mide_la_fuga_en_db_y_las_notas_del_bajo(banco, tmp_path):
    refs = referencias(banco, tmp_path)
    filas, _ = bs.medir_tema(banco.src, banco.salida, ["demucs", "hibrido"], banco.tempos, refs)
    demucs, hibrido = filas
    assert hibrido["fuga_voz_db"] == pytest.approx(20 * np.log10(0.3), abs=0.5)   # el 30 % que dejó pasar
    assert hibrido["desfase_inst_ms"] == 0.0
    assert demucs["fuga_voz_db"] < -30.0
    for f in filas:
        assert (f["bajo_f1_ref"], f["bajo_notas_ref"]) == (0.5, 2)     # acierta una nota de dos


def test_medir_tema_con_referencia_solo_del_bajo(banco, tmp_path):
    refs = referencias(banco, tmp_path, pistas=("bass",))
    filas, _ = bs.medir_tema(banco.src, banco.salida, ["hibrido"], banco.tempos, refs)
    assert "fuga_voz_db" not in filas[0] and filas[0]["bajo_f1_ref"] == 0.5


def test_medir_tema_sin_carpeta_de_referencias_para_ese_tema(banco, tmp_path):
    (tmp_path / "refs" / "otro-tema").mkdir(parents=True)
    filas, _ = bs.medir_tema(banco.src, banco.salida, ["hibrido"], banco.tempos, tmp_path / "refs")
    assert "fuga_voz_db" not in filas[0] and "bajo_f1_ref" not in filas[0]


def test_medir_tema_con_un_motor_que_no_entrega_voz_bateria_ni_bajo(banco):
    banco.motores["demucs"] = {"other": banco.otro, "piano": banco.bajo}
    filas, _ = bs.medir_tema(banco.src, banco.salida, ["demucs"], banco.tempos, None)
    f = filas[0]
    for columna in ("fuga_voz_corr", "bombo_desvio_ms", "fase_bateria_ms", "bajo_notas"):
        assert columna not in f
    # Sin batería no hay fase medida: los loops usan el ancla del tema.
    assert {x[3] for x in banco.loops} == {ANCLA_MS}
    assert (f["loops_aprobados"], f["loops_ventanas"], f["loops_pct"]) == (1, 2, 50.0)


def test_medir_tema_con_bateria_sin_bombos_usa_el_ancla(banco):
    banco.motores["hibrido"]["drums"] = np.zeros_like(banco.bat)
    filas, _ = bs.medir_tema(banco.src, banco.salida, ["hibrido"], banco.tempos, None)
    f = filas[0]
    assert f["fase_bateria_ms"] is None and f["bombo_desvio_ms"] is None and f["bombos"] == 0
    assert {x[3] for x in banco.loops} == {ANCLA_MS}


def test_medir_tema_un_tema_muy_corto_no_tiene_ventanas_ni_porcentaje(banco):
    corto = int(6 * SR)       # 3 compases
    banco.audio[str(banco.src)] = banco.mezcla[:corto]
    banco.motores["hibrido"] = {k: v[:corto] for k, v in banco.motores["hibrido"].items()}
    filas, _ = bs.medir_tema(banco.src, banco.salida, ["hibrido"], banco.tempos, None)
    f = filas[0]
    assert (f["loops_aprobados"], f["loops_ventanas"], f["loops_pct"]) == (0, 0, None)
    assert banco.loops == []


def test_medir_tema_si_un_motor_falla_no_hay_filas_a_medias(banco, monkeypatch):
    def separar_medido(motor, src, out):
        raise RuntimeError("hibrido falló (137): Killed")

    monkeypatch.setattr(bs, "separar_medido", separar_medido)
    with pytest.raises(RuntimeError, match="Killed"):
        bs.medir_tema(banco.src, banco.salida, ["hibrido"], banco.tempos, None)


# ───────────────────────────── main ─────────────────────────────
@pytest.fixture
def corrida(monkeypatch, tmp_path):
    """`main` con `medir_tema` y `escucha_ciega` de mentira: guarda con qué los llamó."""
    c = types.SimpleNamespace(medidos=[], ciegas=[], salida=tmp_path / "banco", fallan=set(), extra={})

    def medir_tema(src, salida, motores, tempos, referencias):
        c.medidos.append((src, salida, list(motores), dict(tempos), referencias))
        if src.stem in c.fallan:
            raise RuntimeError(f"{src.stem} no se pudo separar")
        filas = [{"tema": src.stem, "motor": m, "engine_version": f"{m}-v1", "segundos": 10.0 * (i + 1),
                  **c.extra.get((src.stem, m), {})} for i, m in enumerate(motores)]
        return filas, {m: {"pistas": {}} for m in motores}

    def escucha_ciega(salida, tema, runs, clave, rnd):
        c.ciegas.append((tema, rnd.random()))
        clave[tema] = dict(zip("AB", runs))

    monkeypatch.setattr(bs, "medir_tema", medir_tema)
    monkeypatch.setattr(bs, "escucha_ciega", escucha_ciega)

    def correr(*args):
        monkeypatch.setattr(sys, "argv", ["banco", *map(str, args), "--salida", str(c.salida)])
        bs.main()

    c.correr = correr
    return c


def leer_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def test_main_deja_resultados_clave_y_resumen(corrida, capsys):
    corrida.correr("temas/uno.mp3", "temas/dos.mp3")
    filas = leer_csv(corrida.salida / "resultados.csv")
    assert [(f["tema"], f["motor"]) for f in filas] == [("uno", "demucs"), ("uno", "hibrido"),
                                                        ("dos", "demucs"), ("dos", "hibrido")]
    clave = json.loads((corrida.salida / "clave_ciega.json").read_text())
    assert clave == {"uno": {"A": "demucs", "B": "hibrido"}, "dos": {"A": "demucs", "B": "hibrido"}}
    resumen = (corrida.salida / "resumen.txt").read_text()
    assert "temas: 2" in resumen and "[demucs] demucs-v1" in resumen and "[hibrido] hibrido-v1" in resumen
    assert resumen.strip() in capsys.readouterr().out
    # Por defecto: los dos motores, sin tempos y sin referencias.
    assert corrida.medidos[0] == (Path("temas/uno.mp3"), corrida.salida, ["demucs", "hibrido"], {}, None)


def test_main_un_tema_que_falla_no_pierde_los_demas(corrida, capsys):
    corrida.fallan = {"dos"}
    corrida.correr("uno.mp3", "dos.mp3", "tres.mp3")
    assert [s[0].stem for s in corrida.medidos] == ["uno", "dos", "tres"]
    assert {f["tema"] for f in leer_csv(corrida.salida / "resultados.csv")} == {"uno", "tres"}
    assert set(json.loads((corrida.salida / "clave_ciega.json").read_text())) == {"uno", "tres"}
    assert "dos.mp3: FALLÓ" in capsys.readouterr().out


def test_main_guarda_despues_de_cada_tema(corrida, monkeypatch):
    """Si la corrida se corta a la mitad, lo ya medido está en el disco."""
    vistos = []
    medir = bs.medir_tema

    def medir_y_mirar(src, salida, *resto):
        csv_ = salida / "resultados.csv"
        vistos.append(len(leer_csv(csv_)) if csv_.exists() else None)
        return medir(src, salida, *resto)

    monkeypatch.setattr(bs, "medir_tema", medir_y_mirar)
    corrida.correr("uno.mp3", "dos.mp3", "tres.mp3")
    assert vistos == [None, 2, 4]


def test_main_si_todo_falla_lo_dice(corrida, capsys):
    corrida.fallan = {"uno"}
    corrida.correr("uno.mp3")
    assert (corrida.salida / "resumen.txt").read_text() == "sin resultados\n"
    assert leer_csv(corrida.salida / "resultados.csv") == []
    assert json.loads((corrida.salida / "clave_ciega.json").read_text()) == {}
    assert "sin resultados" in capsys.readouterr().out


def test_main_las_columnas_son_la_union_de_todas_las_filas(corrida):
    corrida.extra = {("dos", "hibrido"): {"fuga_voz_db": -31.5}}
    corrida.correr("uno.mp3", "dos.mp3")
    filas = leer_csv(corrida.salida / "resultados.csv")
    assert list(filas[0])[-1] == "fuga_voz_db"                    # la columna nueva va al final
    assert [f["fuga_voz_db"] for f in filas] == ["", "", "", "-31.5"]


def test_main_lee_tempos_csv(corrida, tmp_path):
    tempos = tmp_path / "tempos.csv"
    tempos.write_text("archivo,bpm,ancla_ms\nuno.mp3,124,35.5\ndos.mp3,128.5,\n")
    corrida.correr("uno.mp3", "--tempos", tempos)
    assert corrida.medidos[0][3] == {"uno.mp3": (124.0, 35.5), "dos.mp3": (128.5, 0.0)}   # sin ancla: 0


def test_main_tempos_sin_la_columna_del_ancla(corrida, tmp_path):
    tempos = tmp_path / "tempos.csv"
    tempos.write_text("archivo,bpm\nuno.mp3,126\n")
    corrida.correr("uno.mp3", "--tempos", tempos)
    assert corrida.medidos[0][3] == {"uno.mp3": (126.0, 0.0)}


def test_main_motores_y_referencias(corrida, tmp_path):
    corrida.correr("uno.mp3", "--motores", " hibrido ,", "--referencias", tmp_path / "refs")
    src, _, motores, _, refs = corrida.medidos[0]
    assert motores == ["hibrido"] and refs == tmp_path / "refs"
    assert [f["motor"] for f in leer_csv(corrida.salida / "resultados.csv")] == ["hibrido"]


def test_main_con_semilla_el_sorteo_se_repite(corrida):
    corrida.correr("uno.mp3", "dos.mp3", "--semilla", "7")
    primera = list(corrida.ciegas)
    corrida.ciegas.clear()
    corrida.correr("uno.mp3", "dos.mp3", "--semilla", "7")
    rnd = random.Random(7)
    assert corrida.ciegas == primera == [("uno", rnd.random()), ("dos", rnd.random())]


def test_main_crea_la_carpeta_de_salida(corrida):
    corrida.salida = corrida.salida / "nueva" / "carpeta"
    corrida.correr("uno.mp3")
    assert (corrida.salida / "resumen.txt").is_file()


def test_main_con_separar_solo_separa(corrida, monkeypatch, tmp_path):
    """El modo interno del subproceso: separa y sale, sin medir ni escribir resultados."""
    pedidos = []
    monkeypatch.setattr(bs, "_separar_en_este_proceso", lambda motor, src, out: pedidos.append((motor, src, out)))
    corrida.correr("--_separar", "hibrido", tmp_path / "tema.mp3", tmp_path / "out")
    assert pedidos == [("hibrido", tmp_path / "tema.mp3", tmp_path / "out")]
    assert corrida.medidos == [] and not corrida.salida.exists()
