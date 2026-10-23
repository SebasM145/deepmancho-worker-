"""#1064 paso 3 · bancos/cues_banco_mezcla.py: los cues de otra versión del worker para el banco
de mezcla de dj-connect, sin desplegar ni reanalizar en producción.

La caché variante solo sirve si cambia lo mismo que cambiaría un reanálisis de verdad: los cues
que guardaría analyze() y nada más, con las mismas reglas de `worker-result` para no pisar los
del DJ. Aquí se fija eso con audio sintético, sin ffmpeg, sin red y sin el audio de nadie."""
import importlib.util
import json
import os
import re
import subprocess
import sys

import numpy as np
import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
import worker  # noqa: E402

_spec = importlib.util.spec_from_file_location("cues_banco_mezcla", os.path.join(RAIZ, "bancos", "cues_banco_mezcla.py"))
cbm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cbm)

SR = worker.SR
BPM = 126.0
BAR_S = 240.0 / BPM

# Los de src/lib/cuesOrigen.test.ts de dj-connect.
ESTRUCTURA = [{"number": n, "positionMs": n * 1000, "energy": 5, "confidence": 0.9} for n in (0, 1, 7)]
GRILLA = [{"number": n, "positionMs": n * 1000, "energy": 5, "confidence": 0.3, "origen": "grilla"} for n in (0, 7)]
TIEMPO = [{"number": n, "positionMs": n * 1000, "energy": 5, "confidence": 0.2, "origen": "tiempo"} for n in (0, 7)]


def tema(partes, intro_sin_bombo=0):
    """El de test_860_cues_mik.py: partes = [(compases, nivel)], 0 = bombo, 1 = + bajo, 2 = + hats."""
    rng = np.random.default_rng(7)
    trozos = []
    if intro_sin_bombo:
        t = np.arange(int(intro_sin_bombo * BAR_S * SR)) / SR
        trozos.append(0.05 * np.sin(2 * np.pi * 220 * t))
    for n, nivel in partes:
        t = np.arange(int(n * BAR_S * SR)) / SR
        y = 0.8 * ((t % (60.0 / BPM)) < 0.03) * np.sin(2 * np.pi * 55 * t)
        if nivel >= 1:
            y = y + 0.3 * np.sin(2 * np.pi * 110 * t)
        if nivel >= 2:
            y = y + 0.25 * rng.standard_normal(t.size) * ((t % (30.0 / BPM)) < 0.02)
        trozos.append(y)
    return np.concatenate(trozos).astype(np.float32)


LARGO = [(12, 1), (32, 2), (16, 0), (32, 2), (16, 1), (32, 2), (40, 0)]


# ====================================================== las reglas de worker-result

def test_origen_reconoce_plan_a_b_y_c():
    assert cbm.origen_de_cues(ESTRUCTURA) == "structure_v1"
    assert cbm.origen_de_cues(GRILLA) == "grilla_v1"
    assert cbm.origen_de_cues(TIEMPO) == "tiempo_v1"
    assert cbm.origen_de_cues([{"number": 0, "positionMs": 0}]) == "computed_v3"
    assert cbm.origen_de_cues([{"number": 0, "positionMs": 0}], "rekordbox") == "rekordbox"


def test_sin_cues_nuevos_no_toca_nada():
    assert cbm.decidir_cues(None, None, None) is None
    assert cbm.decidir_cues([], "structure_v1", ESTRUCTURA) is None


@pytest.mark.parametrize("origen", ["manual", "plan_generacion", "heredada", "mixedinkey"])
def test_nunca_pisa_los_cues_del_dj_ni_los_importados_de_mik(origen):
    assert cbm.decidir_cues(ESTRUCTURA, origen, ESTRUCTURA) is None
    assert cbm.decidir_cues(ESTRUCTURA, origen, None) is None


def test_un_respaldo_llena_un_tema_vacio():
    assert cbm.decidir_cues(GRILLA, None, None) == (GRILLA, "grilla_v1")
    assert cbm.decidir_cues(TIEMPO, None, None)[1] == "tiempo_v1"


def test_un_respaldo_no_reemplaza_cues_de_mejor_origen_ni_importados():
    assert cbm.decidir_cues(GRILLA, "structure_v1", ESTRUCTURA) is None
    assert cbm.decidir_cues(TIEMPO, "grilla_v1", GRILLA) is None
    assert cbm.decidir_cues(GRILLA, "rekordbox", ESTRUCTURA) is None


def test_el_detector_estructural_siempre_mejora_un_respaldo():
    assert cbm.decidir_cues(ESTRUCTURA, "grilla_v1", GRILLA)[1] == "structure_v1"
    assert cbm.decidir_cues(GRILLA, "tiempo_v1", TIEMPO)[1] == "grilla_v1"
    # y un plan A nuevo reemplaza al plan A viejo: es justo lo que cambia #68/#69
    assert cbm.decidir_cues(ESTRUCTURA, "structure_v1", ESTRUCTURA) == (ESTRUCTURA, "structure_v1")


# ====================================================== rejilla y versión

def test_rejilla_tempo_es_bpm_mas_bpm_fine_y_el_ancla_la_del_worker():
    fila = {"bpm": 126, "bpm_fine": -0.03, "first_beat_detected_ms": 241, "first_beat_offset_ms": 300}
    bpm, fb = cbm.rejilla_de_fila(fila)
    assert bpm == pytest.approx(125.97) and fb == 241.0


def test_rejilla_sin_ancla_medida_usa_la_de_la_fila_y_sin_bpm_da_none():
    assert cbm.rejilla_de_fila({"bpm": 124, "bpm_fine": None, "first_beat_offset_ms": 80}) == (124.0, 80.0)
    assert cbm.rejilla_de_fila({"bpm": None, "first_beat_detected_ms": 0}) == (None, 0.0)
    assert cbm.rejilla_de_fila({"bpm": "x", "bpm_fine": "y"}) == (None, 0.0)


def test_version_del_worker_sale_de_la_linea_del_arranque():
    with open(os.path.join(RAIZ, "worker.py"), encoding="utf-8") as f:
        assert re.fullmatch(r"\d+\.\d+\.\d+", cbm.version_del_worker(f.read()))
    assert cbm.version_del_worker('print("DeepMancho worker iniciado (v7.6.26: el cue B …")') == "7.6.26"
    assert cbm.version_del_worker("nada") == "?"


def test_sin_rama_usa_el_worker_de_este_checkout():
    w, version = cbm.cargar_worker()
    assert w is worker and version != "?"


def test_con_rama_carga_el_worker_py_de_esa_ref_aparte_del_de_este_checkout():
    try:
        subprocess.run(["git", "-C", RAIZ, "rev-parse", "HEAD"], check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("sin git en este checkout")
    w, version = cbm.cargar_worker("HEAD")
    assert w is not worker and w.__name__ == "worker_HEAD"
    assert version == cbm.cargar_worker()[1]
    assert callable(w.detect_cues) and callable(w.cuantizar_cues)
    assert sys.modules["worker"] is worker


# ====================================================== el camino de analyze()

def test_cues_como_analyze_es_el_camino_del_worker():
    y = tema(LARGO)
    esperado = worker.detect_cues(y, SR, BPM, 0.0)
    assert esperado and cbm.origen_de_cues(esperado) == "structure_v1"
    dur_ms = len(y) / SR * 1000.0
    kw = ({"inicio_ms": worker.inicio_audible_ms(y, dur_ms)}
          if "inicio_ms" in cbm.inspect.signature(worker.cuantizar_cues).parameters else {})
    esperado = worker.cuantizar_cues(esperado, BPM, 0.0, dur_ms, None, **kw)
    assert cbm.cues_como_analyze(worker, y, SR, BPM, 0.0) == esperado


def test_audio_corto_cae_al_plan_b_y_sin_bpm_al_plan_c():
    y = tema([(20, 2)])
    assert cbm.origen_de_cues(cbm.cues_como_analyze(worker, y, SR, BPM, 0.0)) == "grilla_v1"
    assert cbm.origen_de_cues(cbm.cues_como_analyze(worker, y, SR, None, 0.0)) == "tiempo_v1"
    assert cbm.origen_de_cues(cbm.cues_como_analyze(worker, y, SR, 300.0, 0.0)) == "tiempo_v1"


class _WorkerFalso:
    """Lo justo para ver qué recibe cuantizar_cues en cada versión."""
    SR = SR

    def __init__(self, con_inicio):
        self.llamadas = []
        if con_inicio:
            def cuantizar_cues(cues, bpm, fb, dur_ms, mix=None, inicio_ms=None):
                self.llamadas.append(inicio_ms)
                return cues
        else:
            def cuantizar_cues(cues, bpm, fb, dur_ms, mix=None):
                self.llamadas.append("sin inicio")
                return cues
        self.cuantizar_cues = cuantizar_cues

    def detect_cues(self, y, sr, bpm, fb):
        return list(ESTRUCTURA)

    def inicio_audible_ms(self, y, dur_ms):
        return 123.0


@pytest.mark.parametrize("con_inicio,esperado", [(True, 123.0), (False, "sin inicio")])
def test_sirve_con_la_firma_de_cuantizar_de_7_6_24_y_la_de_7_6_25(con_inicio, esperado):
    w = _WorkerFalso(con_inicio)
    assert cbm.cues_como_analyze(w, np.zeros(SR * 10, dtype=np.float32), SR, BPM, 0.0) == ESTRUCTURA
    assert w.llamadas == [esperado]


# ====================================================== la caché variante

def _cache(base, audio_de=("auto", "manual", "mik", "corto", "grilla")):
    """Una caché como la que deja `npm run mezcla:banco:bajar` (sin audio de nadie)."""
    lista = base / "0a8b6bfd-8250-4dbc-88da-07c553b66f80"
    (lista / "audio").mkdir(parents=True)
    comun = {"bpm": 126, "bpm_fine": 0.0, "first_beat_detected_ms": 0, "key": "8A",
             "energy_entry": 4, "energy_exit": 6, "stream_mp3_asset_path": "x.mp3"}
    filas = [
        dict(comun, id="auto", position=1, cue_source="structure_v1", cue_points=ESTRUCTURA),
        dict(comun, id="manual", position=2, cue_source="manual", cue_points=ESTRUCTURA),
        dict(comun, id="mik", position=3, cue_source="mixedinkey", cue_points=ESTRUCTURA),
        dict(comun, id="sinaudio", position=4, cue_source="structure_v1", cue_points=ESTRUCTURA),
        dict(comun, id="corto", position=5, cue_source="structure_v1", cue_points=ESTRUCTURA),
        dict(comun, id="grilla", position=6, cue_source="grilla_v1", cue_points=GRILLA),
    ]
    (lista / "filas.json").write_text(json.dumps(filas))
    (lista / "pares.json").write_text(json.dumps([[a["id"], b["id"]] for a, b in zip(filas, filas[1:])]))
    for i in audio_de:
        (lista / "audio" / f"{i}.mp3").write_bytes(b"no es audio")
    return lista, filas


def _cargar(ruta):
    return tema([(20, 2)]) if os.path.basename(ruta) == "corto.mp3" else tema(LARGO)


def test_variante_recalcula_solo_lo_que_recalcularia_worker_result(tmp_path):
    lista, filas = _cache(tmp_path / "cache")
    cambios = cbm.variante(str(lista), str(tmp_path / "v"), worker, cargar=_cargar, log=lambda s: None)
    destino = tmp_path / "v" / lista.name
    nuevas = json.loads((destino / "filas.json").read_text())

    assert [f["id"] for f in nuevas] == [f["id"] for f in filas]
    estados = {c["id"]: c["estado"] for c in cambios}
    assert estados == {"auto": "recalculados", "manual": "intocable (manual)", "mik": "intocable (mixedinkey)",
                       "sinaudio": "sin audio", "corto": "se conservan (respaldo)", "grilla": "recalculados"}
    por_id = {f["id"]: f for f in nuevas}
    esperado = cbm.cues_como_analyze(worker, tema(LARGO), SR, BPM, 0.0)
    assert por_id["auto"]["cue_points"] == esperado and por_id["auto"]["cue_source"] == "structure_v1"
    assert por_id["grilla"]["cue_source"] == "structure_v1"
    for f in filas:
        if f["id"] in ("manual", "mik", "sinaudio", "corto"):
            assert por_id[f["id"]] == f
        # lo demás de la fila (rejilla, energía, rutas) nunca cambia
        assert {k: v for k, v in por_id[f["id"]].items() if not k.startswith("cue_")} == \
               {k: v for k, v in f.items() if not k.startswith("cue_")}
    # la caché original queda como estaba
    assert json.loads((lista / "filas.json").read_text()) == filas


def test_variante_deja_lo_que_el_banco_lee(tmp_path):
    lista, filas = _cache(tmp_path / "cache")
    cbm.variante(str(lista), str(tmp_path / "v"), worker, cargar=_cargar, log=lambda s: None)
    destino = tmp_path / "v" / lista.name
    assert json.loads((destino / "pares.json").read_text()) == json.loads((lista / "pares.json").read_text())
    assert os.path.islink(destino / "audio")
    assert os.path.realpath(destino / "audio") == os.path.realpath(lista / "audio")
    cues = json.loads((destino / "cues.json").read_text())
    assert [c["id"] for c in cues] == [f["id"] for f in filas]
    auto = cues[0]
    assert auto["a_antes_ms"] == 0 and auto["b_antes_ms"] == 1000 and auto["cue_source_ahora"] == "structure_v1"
    assert auto["b_compases"] == pytest.approx((auto["b_ahora_ms"] - 1000) / (BAR_S * 1000), abs=0.06)


def test_variante_se_puede_correr_otra_vez_encima(tmp_path):
    lista, _ = _cache(tmp_path / "cache")
    for _ in range(2):
        cbm.variante(str(lista), str(tmp_path / "v"), worker, cargar=_cargar, log=lambda s: None)
    assert os.path.realpath(tmp_path / "v" / lista.name / "audio") == os.path.realpath(lista / "audio")


def test_una_variante_sobre_otra_enlaza_el_audio_real(tmp_path):
    # 7.6.24 -> 7.6.26 sobre la salida de la primera: el enlace no se encadena
    lista, _ = _cache(tmp_path / "cache")
    cbm.variante(str(lista), str(tmp_path / "v1"), worker, cargar=_cargar, log=lambda s: None)
    cbm.variante(str(tmp_path / "v1" / lista.name), str(tmp_path / "v2"), worker, cargar=_cargar, log=lambda s: None)
    assert os.readlink(tmp_path / "v2" / lista.name / "audio") == os.path.realpath(lista / "audio")


def test_la_salida_no_puede_quedar_en_el_repo_ni_pisar_la_cache(tmp_path):
    lista, _ = _cache(tmp_path / "cache")
    with pytest.raises(SystemExit, match="dentro del repo"):
        cbm.variante(str(lista), os.path.join(RAIZ, "tmp-banco"), worker, cargar=_cargar)
    assert not os.path.exists(os.path.join(RAIZ, "tmp-banco"))
    with pytest.raises(SystemExit, match="caché original"):
        cbm.variante(str(lista), str(tmp_path / "cache"), worker, cargar=_cargar)
    with pytest.raises(SystemExit, match="caché original"):
        cbm.variante(str(lista), str(lista / "dentro"), worker, cargar=_cargar)


def test_comparar_marca_el_b_medido_en_la_entrada_del_bajo():
    nuevos = [dict(ESTRUCTURA[0]), dict(ESTRUCTURA[1], positionMs=1000 + int(8 * BAR_S * 1000), origen="auto")]
    c = cbm.comparar({"id": "t", "cue_source": "structure_v1", "cue_points": ESTRUCTURA}, nuevos, "recalculados", BPM)
    assert c["b_en_bajo"] and c["b_compases"] == pytest.approx(8.0, abs=0.01)
    c = cbm.comparar({"id": "t", "cue_points": None}, nuevos, "recalculados", None)
    assert c["b_antes_ms"] is None and c["b_compases"] is None


def test_main_de_punta_a_punta(tmp_path, monkeypatch, capsys):
    lista, _ = _cache(tmp_path / "cache")
    monkeypatch.setattr(cbm, "cargar_audio", lambda ruta, w: _cargar(ruta))
    cbm.main([str(lista), str(tmp_path / "v")])
    salida = capsys.readouterr().out
    assert "[cues] auto" in salida and "intocable (manual)" in salida
    assert re.search(r"worker \d+\.\d+\.\d+ · temas 6 · recalculados 2", salida)
    assert "B se movió en" in salida
    assert (tmp_path / "v" / lista.name / "filas.json").exists()
