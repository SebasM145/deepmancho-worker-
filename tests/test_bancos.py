"""Los dos bancos de medición (bancos/banco_mik.py y bancos/banco_set.py) no tenían pruebas.
No corren en Railway, pero sus números deciden cosas (la escala de energía de #860, la RAM de
una réplica en #75): si dejan de medir lo que el worker hace de verdad, engañan sin avisar.
Aquí se fija que usan el mismo camino que el worker, sin audio real, sin ffmpeg y sin red."""
import importlib.util
import json
import os
import sys

import numpy as np
import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
import worker  # noqa: E402


def cargar(nombre):
    spec = importlib.util.spec_from_file_location(nombre, os.path.join(RAIZ, "bancos", f"{nombre}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


banco_mik = cargar("banco_mik")
banco_set = cargar("banco_set")

SR = worker.SR
BPM = 126.0


def tema_corto(segundos=40.0):
    t = np.arange(int(segundos * SR)) / SR
    return (0.7 * np.exp(-(t % (60.0 / BPM)) * 30) * np.sin(2 * np.pi * 55 * t)
            + 0.1 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


# ================================================================ banco_mik

@pytest.fixture
def piezas(monkeypatch):
    """Carga, BPM fino y ancla simulados; anota cada llamada."""
    llamadas = {"load": [], "refine": [], "anchor": []}
    y = tema_corto()

    def load(ruta, sr, mono, duration):
        llamadas["load"].append((ruta, sr, mono, duration))
        return (y if sr == worker.SR else np.repeat(y, 2)), sr

    def refine(y22, sr22, bpm):
        llamadas["refine"].append((len(y22), sr22, bpm))
        return 126.03, 3.0, 80

    def anchor(ruta, bpm):
        llamadas["anchor"].append((ruta, bpm))
        return {"ancla_ms": 241.6}
    monkeypatch.setattr(banco_mik.librosa, "load", load)
    monkeypatch.setattr(worker, "refine_bpm", refine)
    monkeypatch.setattr(worker, "compute_anchor", anchor)
    llamadas["y"] = y
    return llamadas


def test_preparar_sigue_el_camino_de_analyze_con_semilla(piezas, tmp_path):
    cache = str(tmp_path / "t.npz")
    y, bpm, fb = banco_mik.preparar("tema.mp3", 126, cache)
    # Las dos cargas de analyze(): a SR para cues y energía, a SR_GRID para el BPM fino.
    assert piezas["load"] == [("tema.mp3", worker.SR, True, worker.MAX_DURATION),
                              ("tema.mp3", worker.SR_GRID, True, worker.MAX_DURATION)]
    assert piezas["refine"] == [(2 * len(piezas["y"]), worker.SR_GRID, 126.0)]
    # El ancla se mide con el BPM fino, igual que en el worker.
    assert piezas["anchor"] == [("tema.mp3", 126.03)]
    assert (bpm, fb) == (126.03, 241.6)
    np.testing.assert_array_equal(y, piezas["y"])
    assert os.path.exists(cache)


def test_preparar_sin_bpm_fino_usa_la_semilla(piezas, tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "refine_bpm", lambda *a: (None, None, 4))
    _, bpm, _ = banco_mik.preparar("tema.mp3", "125", str(tmp_path / "t.npz"))
    assert bpm == 125.0 and piezas["anchor"] == [("tema.mp3", 125.0)]


def test_preparar_con_cache_no_vuelve_a_cargar_el_audio(piezas, tmp_path):
    cache = str(tmp_path / "t.npz")
    primera = banco_mik.preparar("tema.mp3", 126, cache)
    antes = {k: len(piezas[k]) for k in ("load", "refine", "anchor")}
    y, bpm, fb = banco_mik.preparar("otro-que-no-se-abre.mp3", 99, cache)
    assert {k: len(piezas[k]) for k in antes} == antes
    np.testing.assert_array_equal(y, primera[0])
    assert (bpm, fb) == (126.03, pytest.approx(241.6))
    assert y.dtype == np.float32 and isinstance(bpm, float) and isinstance(fb, float)


def test_cues_y_energia_usa_el_detector_y_cuantiza(monkeypatch):
    y = tema_corto()
    vistos = {}

    def detect(y_, sr, bpm, fb):
        vistos["detect"] = (sr, bpm, fb)
        return [{"number": 0, "label": "A", "positionMs": 0, "energy": 4},
                {"number": 3, "label": "D", "positionMs": 10010, "energy": 8}]
    monkeypatch.setattr(worker, "detect_cues", detect)
    monkeypatch.setattr(worker, "cues_respaldo", lambda *a: pytest.fail("no debe usar el plan B"))
    r = banco_mik.cues_y_energia(y, BPM, 240.0)
    assert vistos["detect"] == (worker.SR, BPM, 240.0)
    beat = 60.0 / BPM
    # A al primer downbeat de la rejilla (#930) y el resto al beat más cercano, en segundos.
    assert r["cues"][0] == {"s": 0.24, "energia": 4, "label": "A"}
    assert r["cues"][1]["label"] == "D" and r["cues"][1]["energia"] == 8
    assert (r["cues"][1]["s"] - 0.24) / beat == pytest.approx(round((10.01 - 0.24) / beat), abs=0.005)
    assert isinstance(r["energia"], int) and 1 <= r["energia"] <= 10


def test_cues_y_energia_la_energia_es_la_del_worker():
    y = tema_corto()
    bands = worker.compute_bands(y, SR, worker.BUCKETS)
    esperada = worker.compute_energy(worker.librosa.feature.rms(y=y)[0], bands)
    assert banco_mik.cues_y_energia(y, BPM, 0.0)["energia"] == esperada


def test_cues_y_energia_sin_estructura_cae_al_plan_b(monkeypatch):
    monkeypatch.setattr(worker, "detect_cues", lambda *a: None)
    r = banco_mik.cues_y_energia(tema_corto(), BPM, 100.0)
    assert r["cues"] and r["cues"][0]["label"] == worker.CUE_DEF[0][0]
    assert all(set(c) == {"s", "energia", "label"} for c in r["cues"])
    assert [c["s"] for c in r["cues"]] == sorted(c["s"] for c in r["cues"])


def test_cues_y_energia_sin_ningun_cue_devuelve_lista_vacia(monkeypatch):
    monkeypatch.setattr(worker, "detect_cues", lambda *a: None)
    monkeypatch.setattr(worker, "cues_respaldo", lambda *a: None)
    monkeypatch.setattr(worker, "cuantizar_cues", lambda *a: pytest.fail("sin cues no se cuantiza"))
    assert banco_mik.cues_y_energia(tema_corto(), BPM, 0.0)["cues"] == []


def correr_mik(monkeypatch, tmp_path, banco, archivos):
    audio = tmp_path / "audio"
    audio.mkdir()
    for nombre in archivos:
        (audio / nombre).write_bytes(b"x")
    ruta_banco = tmp_path / "banco.json"
    ruta_banco.write_text(json.dumps(banco), encoding="utf-8")
    salida, cache = tmp_path / "res.json", tmp_path / "cache" / "mik"
    preparados = []

    def preparar(ruta, bpm, ruta_cache):
        preparados.append((os.path.basename(ruta), bpm, os.path.basename(ruta_cache)))
        return np.zeros(4, dtype=np.float32), float(bpm) + 0.02, 100.0
    monkeypatch.setattr(banco_mik, "preparar", preparar)
    monkeypatch.setattr(banco_mik, "cues_y_energia",
                        lambda y, bpm, fb: {"energia": 6, "cues": [{"s": 0.1, "energia": 5, "label": "A"}]})
    monkeypatch.setattr(sys, "argv", ["banco_mik.py", "--banco", str(ruta_banco), "--audio", str(audio),
                                      "--cache", str(cache), "--salida", str(salida)])
    banco_mik.main()
    return json.loads(salida.read_text(encoding="utf-8")), preparados, cache


def fila(tid, orden, bpm=126, energia=6):
    return {"track_id": tid, "orden": orden, "bpm": bpm, "mik": {"energia": energia}}


def test_main_mide_cada_tema_y_avisa_del_que_no_tiene_audio(monkeypatch, tmp_path, capsys):
    banco = [fila("aaaaaaaa-1", 1), fila("bbbbbbbb-2", 2, bpm=128), fila("cccccccc-3", 3)]
    res, preparados, cache = correr_mik(monkeypatch, tmp_path, banco, ["aaaaaaaa-1.mp3", "cccccccc-3.flac"])
    assert list(res) == ["aaaaaaaa-1", "cccccccc-3"]
    assert res["aaaaaaaa-1"] == {"energia": 6, "cues": [{"s": 0.1, "energia": 5, "label": "A"}]}
    assert preparados == [("aaaaaaaa-1.mp3", 126, "aaaaaaaa-1.npz"), ("cccccccc-3.flac", 126, "cccccccc-3.npz")]
    assert cache.is_dir()  # crea la carpeta de cache aunque no exista
    out = capsys.readouterr().out
    assert "falta el audio de bbbbbbbb-2" in out
    assert " 1 aaaaaaaa bpm 126.020 ancla 100 ms · energía 6 (MIK 6) · 1 cues" in out


def test_main_prefiere_mp3_sobre_los_otros_formatos(monkeypatch, tmp_path):
    _, preparados, _ = correr_mik(monkeypatch, tmp_path, [fila("t1", 1)], ["t1.wav", "t1.flac", "t1.mp3", "t1.aiff"])
    assert preparados[0][0] == "t1.mp3"


def test_main_con_banco_vacio_deja_un_json_vacio(monkeypatch, tmp_path):
    res, preparados, _ = correr_mik(monkeypatch, tmp_path, [], [])
    assert res == {} and preparados == []


# ================================================================ banco_set

def test_el_set_del_banco_es_el_que_dice_la_cabecera():
    # 28 temas, 181 min de audio (resubida del 4-oct); un BPM por tema, todos de pista de baile.
    assert len(banco_set.DURACIONES) == len(banco_set.BPMS) == 28
    assert round(sum(banco_set.DURACIONES) / 60) == 181
    assert all(110 <= b <= 135 for b in banco_set.BPMS)
    assert banco_set.SR == worker.SET_SR


def test_tema_tiene_el_largo_real_y_el_formato_del_decodificador(monkeypatch):
    monkeypatch.setattr(banco_set, "DURACIONES", [3, 2])
    monkeypatch.setattr(banco_set, "BPMS", [120, 126])
    x = banco_set.tema(0)
    assert x.shape == (3 * banco_set.SR, 2) and x.dtype == np.float32  # lo que da _decode_pcm
    np.testing.assert_array_equal(x[:, 0], x[:, 1])
    assert 0.1 < float(np.max(np.abs(x))) <= 1.0  # ni mudo ni saturado
    assert banco_set.tema(1).shape == (2 * banco_set.SR, 2)


def test_tema_lleva_un_bombo_por_beat(monkeypatch):
    monkeypatch.setattr(banco_set, "DURACIONES", [4])
    monkeypatch.setattr(banco_set, "BPMS", [120])
    x = banco_set.tema(0)[:, 0]
    # La envolvente del bombo vuelve a empezar cada 0,5 s: 8 arranques en 4 s.
    energia = np.abs(x).reshape(-1, banco_set.SR // 100).mean(axis=1)  # ventanas de 10 ms
    subidas = np.nonzero(np.diff(energia) > 0.05)[0]
    assert len(subidas) == 7  # los 8 golpes menos el primero, que no tiene ventana anterior
    assert np.allclose(np.diff(subidas), 50, atol=1)


@pytest.fixture
def banco_corto(monkeypatch):
    """banco_set con 3 temas de un minuto y sin render de verdad. main() reemplaza piezas de `worker`
    y `requests.put` a mano: se registran antes con monkeypatch para que vuelvan a su sitio."""
    monkeypatch.setattr(banco_set, "DURACIONES", [60, 50, 70])
    monkeypatch.setattr(banco_set, "BPMS", [120, 124, 128])
    for obj, nombre in ((worker, "_bajar_tema"), (worker, "_decode_pcm"), (worker.requests, "put")):
        monkeypatch.setattr(obj, nombre, getattr(obj, nombre))
    visto = {}

    def render(job, tracks, upload_url, result_path):
        visto.update(job=job, tracks=tracks, upload_url=upload_url, result_path=result_path)
        return 170.0, []
    monkeypatch.setattr(worker, "render_set", render)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return visto


def test_main_arma_un_plan_que_el_worker_acepta(banco_corto):
    banco_set.main()
    tracks, trans = banco_corto["tracks"], banco_corto["job"]["spec"]["transiciones"]
    assert [t["id"] for t in tracks] == ["0", "1", "2"]
    assert [t["bpm"] for t in tracks] == [120, 124, 128]
    assert len(trans) == 2
    # Si el plan dejara de ser válido, render_set caería al método viejo sin avisar y el banco
    # mediría otra cosa (sin tempo igualado ni estirado).
    assert worker.plan_valido(tracks, trans)
    t0 = trans[0]
    assert (t0["desde"], t0["hasta"], t0["tipo"]) == ("0", "1", "mezcla")
    assert t0["salida_seg"] == 60 - 40.0 and t0["entrada_seg"] == 0.0
    assert t0["duracion_seg"] == pytest.approx(16 * 240 / 120) and t0["compases"] == 16
    assert t0["rate"] == pytest.approx(120 / 124)             # la entrante baja al tempo de la saliente
    assert t0["release_seg"] == pytest.approx(8 * 240 / 124)
    assert trans[1]["rate"] == pytest.approx(124 / 128)


def test_main_simula_solo_la_red(banco_corto, tmp_path):
    banco_set.main()
    # Descarga falsa: deja en el archivo el número del tema; la decodificación lo convierte en audio.
    destino = tmp_path / "tema.bin"
    worker._bajar_tema("http://banco/2", str(destino))
    assert destino.read_text() == "2"
    audio = worker._decode_pcm(str(destino))
    assert audio.shape == (70 * banco_set.SR, 2) and audio.dtype == np.float32
    subida = worker.requests.put("http://banco/subida", data=b"")
    assert subida.status_code == 200 and subida.raise_for_status() is None


def test_main_imprime_la_tabla_con_los_minutos_y_la_ram(banco_corto, capsys):
    banco_set.main()
    out = capsys.readouterr().out
    assert "### #75 C-2 · set largo en una réplica" in out
    assert "| entrada | set resultante | tiempo de render | RAM pico del proceso | RAM antes del render |" in out
    fila_ = [linea for linea in out.splitlines() if linea.startswith("| 3 temas")][0]
    celdas = [c.strip() for c in fila_.strip("|").split("|")]
    assert celdas[0] == "3 temas · 3 min de audio" and celdas[1] == "2.8 min"
    assert celdas[2].endswith(" s")
    pico, base = float(celdas[3].strip("*").split()[0]), float(celdas[4].split()[0])
    # Memoria del proceso en GB: un valor creíble en Linux (KB) y en macOS (bytes).
    assert 0.01 < base <= pico < 64


def test_main_deja_la_tabla_en_el_resumen_de_github_si_existe(banco_corto, monkeypatch, tmp_path, capsys):
    resumen = tmp_path / "resumen.md"
    resumen.write_text("ya había algo\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(resumen))
    banco_set.main()
    texto = resumen.read_text(encoding="utf-8")
    assert texto.startswith("ya había algo\n")  # agrega, no pisa
    assert texto.endswith(capsys.readouterr().out.rstrip("\n").split("\n")[-1] + "\n")
    assert "| 3 temas · 3 min de audio |" in texto


def test_main_no_deja_piezas_cambiadas_en_el_worker(monkeypatch):
    # Guardia de esta misma prueba: después de `banco_corto` el worker queda como estaba.
    bajar, decode, put = worker._bajar_tema, worker._decode_pcm, worker.requests.put
    with monkeypatch.context() as m:
        m.setattr(banco_set, "DURACIONES", [60, 50])
        m.setattr(banco_set, "BPMS", [120, 124])
        for obj, nombre in ((worker, "_bajar_tema"), (worker, "_decode_pcm"), (worker.requests, "put")):
            m.setattr(obj, nombre, getattr(obj, nombre))
        m.setattr(worker, "render_set", lambda *a: (100.0, []))
        banco_set.main()
        assert worker._bajar_tema is not bajar
    assert (worker._bajar_tema, worker._decode_pcm, worker.requests.put) == (bajar, decode, put)
