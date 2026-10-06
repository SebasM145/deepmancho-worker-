"""worker.process_job: lo que el análisis le manda a la base (W6, relevo de noche 5-oct).

`process_job` arma el resultado que guarda `worker-result`. Sus ramas no tenían
prueba: tema sin audio, pista muda, etiqueta de BPM fuera de rango, género,
tonalidad que no se pisa, ancla CM2 (solo `first_beat_detected_ms`), copia de
escucha, máster de 320k e identificación por huella. Además
`fingerprint_identify`. Sin red ni ffmpeg: el análisis, la descarga y la subida
se reemplazan por espías. Fijan el comportamiento actual.
"""
import json
import os
import signal
import subprocess
import sys

import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import worker  # noqa: E402

ANALISIS = {"bpm": 124.02, "key": "8A", "energy": 7, "duration_seconds": 300,
            "first_beat_offset_ms": 120, "cue_points": [{"label": "A", "positionMs": 120}]}


@pytest.fixture
def job(monkeypatch, tmp_path):
    """Un trabajo con todo lo de afuera simulado. `estado` deja cambiar cada pieza
    y guarda lo que se envió y lo que se llamó."""
    audio = tmp_path / "tema.mp3"
    estado = {"enviados": [], "analisis": dict(ANALISIS), "semillas": [], "genero": {}, "ancla": None,
              "ancla_pedida": [], "rendiciones": [], "subidas": [], "subida_ok": True, "huella": None,
              "huellas": 0, "audio": audio}

    def download_audio(url):
        audio.write_bytes(b"audio")
        return str(audio)

    def analyze(path, bpm_seed=None):
        estado["semillas"].append(bpm_seed)
        if isinstance(estado["analisis"], Exception):
            raise estado["analisis"]
        return dict(estado["analisis"])

    def detectar_genero(path):
        if isinstance(estado["genero"], Exception):
            raise estado["genero"]
        return dict(estado["genero"])

    def ancla(track_id, bpm):
        estado["ancla_pedida"].append((track_id, bpm))
        if isinstance(estado["ancla"], Exception):
            raise estado["ancla"]
        return estado["ancla"]

    def make_rendition(src, bitrate=None, sufijo=".stream", etiquetas=False):
        p = tmp_path / f"r{sufijo}.mp3"
        p.write_bytes(b"r")
        estado["rendiciones"].append({"bitrate": bitrate, "sufijo": sufijo, "etiquetas": etiquetas, "path": p})
        return str(p)

    def upload_rendition(url, path, mime=None):
        estado["subidas"].append(url)
        return estado["subida_ok"]

    def huella(path):
        estado["huellas"] += 1
        return estado["huella"]

    monkeypatch.setattr(worker, "download_audio", download_audio)
    monkeypatch.setattr(worker, "analyze", analyze)
    monkeypatch.setattr(worker, "detectar_genero", detectar_genero)
    monkeypatch.setattr(worker, "medir_sonoridad", lambda p: {"loudness_lufs": -9.5, "energy_v2": 6})
    monkeypatch.setattr(worker, "ancla_de_rendicion", ancla)
    monkeypatch.setattr(worker, "make_rendition", make_rendition)
    monkeypatch.setattr(worker, "upload_rendition", upload_rendition)
    monkeypatch.setattr(worker, "fingerprint_identify", huella)
    monkeypatch.setattr(worker, "send_result", lambda *a, **k: estado["enviados"].append((a, k)))
    monkeypatch.setattr(worker, "ENABLE_ANCHOR_BACKFILL", False)
    monkeypatch.setattr(worker, "EXAMEN_CM2_APROBADO", False)
    yield estado
    signal.alarm(0)


def correr(track=None, url="http://x/tema.mp3", **kw):
    worker.process_job({"id": "j1", "track_id": "t1"}, track if track is not None else {"artist": "A", "title": "B"}, url, **kw)


def resultado(estado):
    (args, kw), = estado["enviados"]
    assert args[:3] == ("j1", "t1", "done"), (args, kw)
    return kw["result"]


# ─────────────────────────────── entrada ───────────────────────────────
def test_sin_audio_avisa_y_no_analiza(job):
    correr(url=None)
    assert job["enviados"] == [(("j1", "t1", "error"), {"error": "track sin audio"})]
    assert job["semillas"] == []


def test_pista_muda_queda_como_vacia(job):
    job["analisis"] = worker.AudioMudo("audio mudo")
    correr()
    assert job["enviados"] == [(("j1", "t1", "done"), {"result": {"pista_vacia": True, "analysis_flags": ["silencio"]}})]
    assert not job["audio"].exists()                 # el temporal se borra
    assert worker.EN_TRABAJO is False


def test_error_del_analisis_avisa_y_suelta_todo(job):
    job["analisis"] = RuntimeError("librosa se cayó")
    correr()
    (args, kw), = job["enviados"]
    assert args == ("j1", "t1", "error") and kw["error"] == "librosa se cayó"
    assert not job["audio"].exists()
    assert worker.EN_TRABAJO is False
    assert signal.alarm(0) == 0                      # el tope por trabajo quedó apagado


def test_al_terminar_apaga_el_tope_y_borra_el_temporal(job):
    correr()
    assert signal.alarm(0) == 0
    assert not job["audio"].exists()
    assert worker.EN_TRABAJO is False


# ─────────────────────────────── tempo y tonalidad ───────────────────────────────
def test_bpm_de_la_etiqueta_es_la_semilla(job):
    correr({"bpm": 124, "artist": "A", "title": "B"})
    assert job["semillas"] == [124]
    assert "analysis_flags" not in resultado(job)


def test_etiqueta_fuera_de_rango_se_analiza_en_su_octava_y_se_marca(job):
    job["analisis"]["analysis_flags"] = ["tempo_variable"]
    correr({"bpm": 240, "artist": "A", "title": "B"})
    assert job["semillas"] == [120.0]
    assert resultado(job)["analysis_flags"] == ["tempo_variable", "bpm_etiqueta_octava:240→120"]


def test_sin_etiqueta_no_hay_semilla(job):
    correr()
    assert job["semillas"] == [None]


def test_siempre_manda_el_bpm_medido(job):
    correr({"bpm": 124, "artist": "A", "title": "B"})
    assert resultado(job)["bpm"] == 124.02


def test_la_tonalidad_de_la_etiqueta_no_se_pisa(job):
    correr({"key": "5A", "artist": "A", "title": "B"})
    assert resultado(job)["key"] is None


def test_sin_tonalidad_manda_la_medida(job):
    correr()
    assert resultado(job)["key"] == "8A"


# ─────────────────────────────── género y sonoridad ───────────────────────────────
def test_genero_por_revisar_va_como_marca_y_no_como_campo(job):
    job["genero"] = {"genre_detected": "House", "genre_confidence": 0.4, "genero_por_revisar": True}
    correr()
    r = resultado(job)
    assert r["genre_detected"] == "House" and r["genre_confidence"] == 0.4
    assert "genero_por_revisar" not in r and r["analysis_flags"] == ["genero_por_revisar"]


def test_si_falla_el_genero_el_analisis_sigue(job):
    job["genero"] = RuntimeError("mutagen")
    correr()
    r = resultado(job)
    assert "genre_detected" not in r and r["loudness_lufs"] == -9.5 and r["energy_v2"] == 6


# ─────────────────────────────── CM2 (ancla de precisión) ───────────────────────────────
def con_cm2(monkeypatch):
    monkeypatch.setattr(worker, "ENABLE_ANCHOR_BACKFILL", True)
    monkeypatch.setattr(worker, "EXAMEN_CM2_APROBADO", True)


def test_cm2_apagado_no_pide_ancla(job, monkeypatch):
    monkeypatch.setattr(worker, "ENABLE_ANCHOR_BACKFILL", True)     # variable sí, examen no
    correr({"bpm": 124, "artist": "A", "title": "B"})
    assert job["ancla_pedida"] == [] and "first_beat_detected_ms" not in resultado(job)


def test_cm2_con_residuo_bajo_escribe_solo_el_ancla_detectada(job, monkeypatch):
    con_cm2(monkeypatch)
    job["ancla"] = {"ancla_ms": 251.6, "residuo_ms": 8.0}
    correr({"bpm": 248, "artist": "A", "title": "B"})
    assert job["ancla_pedida"] == [("t1", 124.0)]    # el BPM de la etiqueta, en su octava
    r = resultado(job)
    assert r["first_beat_detected_ms"] == 252
    assert r["first_beat_offset_ms"] == 120          # el del análisis; CM2 no lo toca
    assert not any(k.endswith("_source") for k in r)


def test_cm2_sin_etiqueta_usa_el_bpm_medido(job, monkeypatch):
    con_cm2(monkeypatch)
    job["ancla"] = {"ancla_ms": 100.0, "residuo_ms": 1.0}
    correr()
    assert job["ancla_pedida"] == [("t1", 124.02)]


def test_cm2_con_residuo_alto_no_escribe(job, monkeypatch):
    con_cm2(monkeypatch)
    job["ancla"] = {"ancla_ms": 251.6, "residuo_ms": 8.1}
    correr()
    assert "first_beat_detected_ms" not in resultado(job)


def test_cm2_que_falla_no_bloquea_el_trabajo(job, monkeypatch):
    con_cm2(monkeypatch)
    job["ancla"] = RuntimeError("stream-track 404")
    correr()
    r = resultado(job)
    assert "first_beat_detected_ms" not in r and r["energy"] == 7


# ─────────────────────────────── copia de escucha y máster ───────────────────────────────
SUBIDA = {"url": "https://x/upload/stream.mp3", "path": "t1/stream.mp3"}
MASTER = {"url": "https://x/upload/master.mp3", "path": "t1/master.mp3"}


def test_copia_de_escucha_subida_queda_en_el_resultado(job):
    correr(rendition_upload=SUBIDA)
    assert job["subidas"] == [SUBIDA["url"]]
    assert job["rendiciones"][0]["sufijo"] == ".stream" and job["rendiciones"][0]["etiquetas"] is False
    assert resultado(job)["rendition_path"] == "t1/stream.mp3"
    assert not job["rendiciones"][0]["path"].exists()    # el archivo local se borra


def test_copia_de_escucha_que_no_sube_no_se_anota(job):
    job["subida_ok"] = False
    correr(rendition_upload=SUBIDA)
    assert "rendition_path" not in resultado(job)


@pytest.mark.parametrize("subida", [None, {"url": "https://x/u"}, {"path": "t1/s.mp3"}])
def test_sin_destino_completo_no_hay_copia(job, subida):
    correr(rendition_upload=subida)
    assert job["rendiciones"] == [] and "rendition_path" not in resultado(job)


MASTER_FLAC = {"url": "https://x/upload/master.flac", "path": "t1/master.flac"}


@pytest.fixture
def flac(job, monkeypatch, tmp_path):
    """Original PCM y un FLAC verificado simulados (la conversión real se prueba con ffmpeg
    en tests/test_master_flac.py)."""
    job["codec"] = "pcm_s24le"
    job["flacs"] = []
    monkeypatch.setattr(worker, "codec_de", lambda path: job["codec"])

    def make_master_flac(src):
        p = tmp_path / "m.master.flac"
        p.write_bytes(b"f" * 1234)
        job["flacs"].append(src)
        return str(p)
    monkeypatch.setattr(worker, "make_master_flac", make_master_flac)
    return job


def test_master_solo_si_el_tema_lo_necesita(flac):
    correr({"artist": "A", "title": "B"}, master_upload=MASTER_FLAC)
    assert flac["flacs"] == []
    correr({"artist": "A", "title": "B", "needs_master_conversion": True}, master_upload=MASTER_FLAC)
    assert len(flac["flacs"]) == 1 and flac["rendiciones"] == []        # nunca un MP3 de master
    r = flac["enviados"][1][1]["result"]
    assert (r["master_path"], r["master_mime"], r["master_bytes"]) == ("t1/master.flac", "audio/flac", 1234)


def test_master_que_no_sube_no_se_anota(flac):
    flac["subida_ok"] = False
    correr({"artist": "A", "title": "B", "needs_master_conversion": True}, master_upload=MASTER_FLAC)
    assert "master_path" not in resultado(flac)


def test_plataforma_vieja_que_pide_master_mp3_no_convierte(flac):
    correr({"artist": "A", "title": "B", "needs_master_conversion": True}, master_upload=MASTER)
    assert flac["flacs"] == [] and flac["rendiciones"] == [] and "master_path" not in resultado(flac)


@pytest.mark.parametrize("codec", ["mp3", "aac", "flac", "pcm_f32le", "pcm_s32le", None])
def test_lo_que_no_es_pcm_entero_queda_tal_cual(flac, codec):
    flac["codec"] = codec
    correr({"artist": "A", "title": "B", "needs_master_conversion": True}, master_upload=MASTER_FLAC)
    assert flac["flacs"] == [] and "master_path" not in resultado(flac)


def test_flac_que_no_verifica_no_reemplaza(flac, monkeypatch):
    monkeypatch.setattr(worker, "make_master_flac", lambda src: None)
    correr({"artist": "A", "title": "B", "needs_master_conversion": True}, master_upload=MASTER_FLAC)
    assert "master_path" not in resultado(flac) and flac["subidas"] == []


# ─────────────────────────────── identificación por huella ───────────────────────────────
@pytest.mark.parametrize("track", [{"artist": "A", "title": "B"}])
def test_con_artista_y_titulo_no_busca_la_huella(job, track):
    correr(track)
    assert job["huellas"] == 0


@pytest.mark.parametrize("track", [{}, {"artist": "A"}, {"title": "B"}, {"artist": "", "title": "B"}])
def test_sin_artista_o_titulo_busca_la_huella(job, track):
    job["huella"] = {"artist": "Kerri Chandler", "title": "Rain"}
    correr(track)
    r = resultado(job)
    assert job["huellas"] == 1
    assert (r["identified_artist"], r["identified_title"]) == ("Kerri Chandler", "Rain")
    assert "artist" not in r and "title" not in r      # solo los campos identified_*


def test_huella_sin_resultado_no_agrega_campos(job):
    correr({})
    assert not any(k.startswith("identified_") for k in resultado(job))


# ─────────────────────────────── fingerprint_identify ───────────────────────────────
@pytest.fixture
def acoustid(monkeypatch):
    estado = {"fpcalc": subprocess.CompletedProcess([], 0, json.dumps({"fingerprint": "AQAA", "duration": 301.6}), ""),
              "respuesta": {"results": []}, "http_ok": True, "pedidos": []}
    monkeypatch.setattr(worker, "ACOUSTID_API_KEY", "clave")

    def run(cmd, **k):
        if isinstance(estado["fpcalc"], Exception):
            raise estado["fpcalc"]
        return estado["fpcalc"]

    class R:
        def raise_for_status(self):
            if not estado["http_ok"]:
                raise RuntimeError("HTTP 503")

        def json(self):
            return estado["respuesta"]

    def get(url, params=None, headers=None, timeout=None):
        estado["pedidos"].append(params)
        return R()

    monkeypatch.setattr(worker.subprocess, "run", run)
    monkeypatch.setattr(worker.requests, "get", get)
    return estado


def test_huella_sin_clave_no_hace_nada(acoustid, monkeypatch):
    monkeypatch.setattr(worker, "ACOUSTID_API_KEY", "")
    assert worker.fingerprint_identify("a.mp3") is None and acoustid["pedidos"] == []


def test_huella_toma_el_mejor_puntaje_con_artista_y_titulo(acoustid):
    acoustid["respuesta"] = {"results": [
        {"score": 0.5, "recordings": [{"title": "Otra", "artists": [{"name": "X"}]}]},
        {"score": 0.9, "recordings": [{"title": "Sin artista", "artists": []},
                                      {"title": " Rain ", "artists": [{"name": " Kerri Chandler "}, {"name": "Y"}]}]},
    ]}
    assert worker.fingerprint_identify("a.mp3") == {"artist": "Kerri Chandler", "title": "Rain"}
    p, = acoustid["pedidos"]
    assert p["duration"] == 302 and p["fingerprint"] == "AQAA" and p["client"] == "clave"


@pytest.mark.parametrize("fpcalc", [
    subprocess.CompletedProcess([], 1, "", "error"),
    subprocess.CompletedProcess([], 0, "", ""),
    subprocess.CompletedProcess([], 0, json.dumps({"fingerprint": "", "duration": 300}), ""),
    subprocess.CompletedProcess([], 0, json.dumps({"fingerprint": "AQAA", "duration": 0}), ""),
])
def test_huella_sin_datos_de_fpcalc_no_consulta(acoustid, fpcalc):
    acoustid["fpcalc"] = fpcalc
    assert worker.fingerprint_identify("a.mp3") is None and acoustid["pedidos"] == []


def test_huella_sin_fpcalc_avisa_y_sigue(acoustid, capsys):
    acoustid["fpcalc"] = FileNotFoundError("fpcalc")
    assert worker.fingerprint_identify("a.mp3") is None
    assert "fpcalc no encontrado" in capsys.readouterr().out


@pytest.mark.parametrize("cambio", [{"http_ok": False}, {"respuesta": {"results": [{"score": 1, "recordings": [{"title": "", "artists": [{"name": "X"}]}]}]}}])
def test_huella_con_error_o_sin_coincidencia_da_none(acoustid, cambio):
    acoustid.update(cambio)
    assert worker.fingerprint_identify("a.mp3") is None
