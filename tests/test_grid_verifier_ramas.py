"""W6 (3-oct-2026): ramas del verificador de rejilla que no tenían prueba.

- `phase_at_tempo`: las cinco salidas de la puerta de fase (port de gridVerify.ts).
- Casos borde de `coverage`, `verify_grid_whole`, `passes_grid_gate` y `measure_track`.
- `probe_sample_rate`/`decode_file`, `download`, `api`, `process_one` y `main_loop`
  sin red ni ffmpeg (todo simulado).
"""
import io
import json
import math
import os
import subprocess
import sys
import urllib.error

import numpy as np
import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import grid_verifier as gv  # noqa: E402

# ── phase_at_tempo ───────────────────────────────────────────────────────────
BIN_RATE = 500.0
BPM = 120.0
PERIODO_BINS = 250  # 0,5 s a 500 bins/s
PERIODO_S = 0.5


def golpes(*fases_y_pesos, beats=64):
    """Ataques en las fases pedidas (fracción del beat) con su peso, en cada beat."""
    osf = np.zeros(beats * PERIODO_BINS, dtype=np.float32)
    for fase, peso in fases_y_pesos:
        off = round(fase * PERIODO_BINS)
        osf[off::PERIODO_BINS] += peso
    return osf


def test_fase_plana_conserva_el_ancla():
    r = gv.phase_at_tempo(np.ones(64 * PERIODO_BINS, dtype=np.float32), BIN_RATE, BPM, 0.05)
    assert r["reason"] == "phase_flat"
    assert r["ok"] is False
    assert r["anchorSec"] == 0.05 and r["deltaSec"] == 0.0


def test_fase_cerca_del_ancla_la_corrige_poco():
    r = gv.phase_at_tempo(golpes((0.1, 1.0)), BIN_RATE, BPM, 0.1 * PERIODO_S)
    assert r["reason"] == "near_prior" and r["ok"] is True
    assert abs(r["deltaSec"]) < 0.005
    assert r["peakRatio"] == 99.0  # un solo pico: sin segundo


def test_fase_cerca_pero_ambigua_no_mueve_el_ancla():
    # Pico cerca del ancla, pero otro casi igual medio beat después (1 / 0,9 < 1,25).
    r = gv.phase_at_tempo(golpes((0.1, 1.0), (0.6, 0.9)), BIN_RATE, BPM, 0.1 * PERIODO_S)
    assert r["reason"] == "phase_ambiguous" and r["ok"] is False
    assert r["anchorSec"] == pytest.approx(0.1 * PERIODO_S)
    assert 1.0 < r["peakRatio"] < gv.PHASE_GATE["minPeakRatio"]


def test_fase_lejos_pero_clara_mueve_el_ancla():
    # El ancla guardada está medio beat corrida y no hay otro pico: se corrige.
    r = gv.phase_at_tempo(golpes((0.6, 1.0)), BIN_RATE, BPM, 0.1 * PERIODO_S)
    assert r["reason"] == "far_strong" and r["ok"] is True
    assert abs(abs(r["deltaSec"]) - 0.5 * PERIODO_S) < 0.005


def test_fase_lejos_y_debil_prefiere_el_pico_local():
    # El pico más alto está lejos (0,6) y no es claro (1 / 0,7 < 2,5); cerca del ancla
    # (0,12) hay uno que pesa ≥ 60 % del máximo: gana el local.
    r = gv.phase_at_tempo(golpes((0.6, 1.0), (0.12, 0.7)), BIN_RATE, BPM, 0.1 * PERIODO_S)
    assert r["reason"] == "local_near_prior" and r["ok"] is True
    assert r["anchorSec"] == pytest.approx(0.12 * PERIODO_S, abs=0.005)
    assert r["peakRatio"] == pytest.approx(0.7, abs=0.05)


def test_fase_lejos_y_debil_sin_pico_local_no_mueve_el_ancla():
    # Dos picos lejos del ancla (0,6 y 0,4) y nada cerca de 0,1: se conserva.
    r = gv.phase_at_tempo(golpes((0.6, 1.0), (0.4, 0.5)), BIN_RATE, BPM, 0.1 * PERIODO_S)
    assert r["reason"] == "phase_far_weak" and r["ok"] is False
    assert r["anchorSec"] == pytest.approx(0.1 * PERIODO_S)


# ── Casos borde de la medición ───────────────────────────────────────────────
def test_cobertura_con_audio_mas_corto_que_una_ventana():
    assert gv.coverage(np.ones(10, dtype=np.float32), BIN_RATE) == {"good": 0, "total": 0}


def test_cobertura_cuenta_las_ventanas_con_sonido():
    env = np.ones(10 * 8 * 500, dtype=np.float32)
    env[: 2 * 8 * 500] = 0.01  # las dos primeras ventanas casi en silencio
    assert gv.coverage(env, BIN_RATE) == {"good": 8, "total": 10}


@pytest.mark.parametrize("candidatos,segundos", [
    ([], 60),
    ([30, 250, -1], 60),  # todos fuera de 40–220
    ([124], 19),          # menos de 20 s de audio
])
def test_verify_grid_whole_sin_nada_que_medir(candidatos, segundos):
    env = np.ones(int(segundos * BIN_RATE), dtype=np.float32)
    assert gv.verify_grid_whole(env, BIN_RATE, 0.0, candidatos) is None


def test_verify_grid_whole_descarta_candidatos_repetidos(monkeypatch):
    vistos = []
    monkeypatch.setattr(gv, "scan_tempo", lambda osf, br, centros, *a: vistos.append(centros) or
                        {"bpm": centros[0], "sharp": 2.0})
    env = np.ones(int(30 * BIN_RATE), dtype=np.float32)
    r = gv.verify_grid_whole(env, BIN_RATE, 0.0, [124.0, 124.3, 300, 128.0, 127.8])
    assert vistos == [[124.0, 128.0]]
    assert r["candidateBpm"] == 124.0


def test_puerta_de_rejilla_sin_resultado():
    assert gv.passes_grid_gate(None) is False
    assert gv.passes_grid_gate({}) is False


@pytest.mark.parametrize("track,dur_s,error", [
    ({"bpm": None, "first_beat_detected_ms": 100}, 60, "no_bpm"),
    ({"bpm": 124, "first_beat_detected_ms": None}, 60, "no_anchor"),
    ({"bpm": 124, "first_beat_detected_ms": 100}, 29, "too_short"),
])
def test_measure_track_errores_deterministas(track, dur_s, error):
    sr = 1000
    with pytest.raises(RuntimeError, match=f"^{error}$"):
        gv.measure_track(track, np.zeros(dur_s * sr, dtype=np.float32), sr)
    assert gv.codigo_error(RuntimeError(error)).endswith(error)


def test_measure_track_sin_ajuste_es_no_fit(monkeypatch):
    monkeypatch.setattr(gv, "verify_grid_whole", lambda *a, **k: None)
    sr = 1000
    with pytest.raises(RuntimeError, match="^no_fit$"):
        gv.measure_track({"bpm": 124, "first_beat_detected_ms": 100}, np.zeros(40 * sr, np.float32), sr)


def test_measure_track_sin_bpm_guardado_usa_el_detectado(monkeypatch):
    """Sin `bpm` en el catálogo, el entero sale de la medida y bpm_fine queda en ±0,5."""
    vistos = []

    def falso(env, br, prior, centros):
        vistos.append(centros)
        return {"anchorMs": 100, "bpm": 125.7, "conf": 0.9, "peakRatio": 3.0, "windowsGood": 7,
                "windowsTotal": 8, "residualBeats": 0}

    monkeypatch.setattr(gv, "verify_grid_whole", falso)
    sr = 1000
    m = gv.measure_track({"bpm": None, "bpm_detected": 125.6, "first_beat_detected_ms": 100},
                         np.zeros(40 * sr, np.float32), sr)
    assert vistos == [[125.6, 125.6]]  # el detectado entra como catálogo y como candidato
    assert m["bpmFine"] == pytest.approx(-0.3)
    assert m["passesGate"] is True


# ── ffprobe / ffmpeg simulados ───────────────────────────────────────────────
class Proceso:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_decode_file_devuelve_pcm_a_la_tasa_nativa(monkeypatch):
    pcm = np.array([0.0, 0.5, -0.5], dtype=np.float32)
    llamadas = []

    def run(cmd, **kw):
        llamadas.append(cmd)
        if cmd[0] == "ffprobe":
            return Proceso(stdout="48000\n", stderr="")
        return Proceso(stdout=pcm.tobytes())

    monkeypatch.setattr(subprocess, "run", run)
    out, sr = gv.decode_file("x.mp3")
    assert sr == 48000
    assert np.array_equal(out, pcm)
    ffmpeg = llamadas[1]
    assert ffmpeg[ffmpeg.index("-ar") + 1] == "48000" and ffmpeg[ffmpeg.index("-ac") + 1] == "1"


@pytest.mark.parametrize("rc,salida", [(1, "48000"), (0, ""), (0, "0")])
def test_ffprobe_que_falla(monkeypatch, rc, salida):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Proceso(rc, salida, "sin flujo de audio"))
    with pytest.raises(RuntimeError, match="^ffprobe_failed:"):
        gv.probe_sample_rate("x.mp3")


def test_ffmpeg_que_falla(monkeypatch):
    def run(cmd, **kw):
        if cmd[0] == "ffprobe":
            return Proceso(stdout="44100", stderr="")
        return Proceso(1, b"", b"Invalid data found when processing input")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(RuntimeError, match="^ffmpeg_failed:Invalid data"):
        gv.decode_file("x.mp3")


# ── Red simulada ─────────────────────────────────────────────────────────────
class Respuesta(io.BytesIO):
    def __init__(self, cuerpo: bytes, largo=None):
        super().__init__(cuerpo)
        self.headers = {} if largo is None else {"content-length": str(largo)}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_download_guarda_el_archivo(monkeypatch, tmp_path):
    monkeypatch.setattr(gv.urllib.request, "urlopen", lambda req, timeout: Respuesta(b"abc", 3))
    dest = tmp_path / "in.mp3"
    assert gv.download("http://audio.invalid/x", str(dest)) == 3
    assert dest.read_bytes() == b"abc"


def test_download_corta_por_content_length(monkeypatch, tmp_path):
    monkeypatch.setattr(gv, "MAX_MB", 5)
    monkeypatch.setattr(gv.urllib.request, "urlopen",
                        lambda req, timeout: Respuesta(b"", 5 * 1024 * 1024 + 1))
    dest = tmp_path / "in.mp3"
    with pytest.raises(RuntimeError, match="^file_too_large:"):
        gv.download("http://audio.invalid/x", str(dest))
    assert not dest.exists()


def test_download_corta_sin_content_length(monkeypatch, tmp_path):
    """Sin cabecera de largo, igual se corta al leer un byte de más."""
    monkeypatch.setattr(gv, "MAX_MB", 5)
    cuerpo = b"x" * (5 * 1024 * 1024 + 10)
    monkeypatch.setattr(gv.urllib.request, "urlopen", lambda req, timeout: Respuesta(cuerpo))
    with pytest.raises(RuntimeError, match=f"^file_too_large:{5 * 1024 * 1024 + 1}$"):
        gv.download("http://audio.invalid/x", str(tmp_path / "in.mp3"))


def test_api_manda_el_secreto_y_traduce_errores_http(monkeypatch):
    pedidos = []

    def urlopen(req, timeout):
        pedidos.append(req)
        if req.full_url.endswith("grid-verify-result"):
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"error":"bad_measurement"}'))
        return Respuesta(json.dumps({"job": None}).encode())

    monkeypatch.setattr(gv, "API", "http://plataforma.invalid")
    monkeypatch.setattr(gv, "SECRET", "s3")
    monkeypatch.setattr(gv.urllib.request, "urlopen", urlopen)
    assert gv.api("grid-verify-next", {}) == {"job": None}
    assert pedidos[0].get_header("X-worker-secret") == "s3"
    assert pedidos[0].full_url == "http://plataforma.invalid/grid-verify-next"
    with pytest.raises(RuntimeError, match='^grid-verify-result 400: {"error":"bad_measurement"}$'):
        gv.api("grid-verify-result", {"ok": True})


# ── process_one ──────────────────────────────────────────────────────────────
class Plataforma:
    def __init__(self, siguiente, falla_reporte=False):
        self.siguiente = siguiente
        self.falla_reporte = falla_reporte
        self.reportes = []

    def api(self, path, body, timeout=30):
        if path == "grid-verify-next":
            return self.siguiente
        if self.falla_reporte:
            raise RuntimeError("grid-verify-result 503: caída")
        self.reportes.append(body)
        return {"ok": True}


TEMA = {"bpm": 124, "bpm_fine": 0, "first_beat_detected_ms": 250, "grid_source": "detectada",
        "duration_seconds": 64, "path": "x.mp3"}
TRABAJO = {"job": {"id": "j1", "track_id": "t1"}, "track": TEMA, "audio_url": "http://audio.invalid/x.mp3"}


def registros(capsys):
    return [json.loads(linea) for linea in capsys.readouterr().out.splitlines() if linea.startswith("{")]


def test_cola_vacia_no_hay_trabajo(monkeypatch):
    monkeypatch.setattr(gv, "api", Plataforma({"job": None}).api)
    assert gv.process_one() is False


def test_trabajos_saltados_cuentan_como_trabajo(monkeypatch, capsys):
    monkeypatch.setattr(gv, "api", Plataforma({"job": None, "skipped": 3}).api)
    assert gv.process_one() is True  # sin esperar: puede haber más en la cola
    assert registros(capsys)[-1]["msg"] == "skipped"


def test_sin_url_de_audio_se_reporta_y_se_puede_reintentar(monkeypatch):
    plat = Plataforma({**TRABAJO, "audio_url": None})
    monkeypatch.setattr(gv, "api", plat.api)
    assert gv.process_one() is True
    assert plat.reportes == [{"job_id": "j1", "track_id": "t1", "ok": False, "error": "no_audio_url"}]


def test_error_de_descarga_se_reporta_sin_la_firma(monkeypatch):
    plat = Plataforma(TRABAJO)
    monkeypatch.setattr(gv, "api", plat.api)

    def download(url, dest):
        raise RuntimeError("HTTP Error 403 for url: https://x.supabase.co/a.mp3?token=eyJabcdefgh.ijklmnop.qrs")

    monkeypatch.setattr(gv, "download", download)
    gv.process_one()
    err = plat.reportes[0]["error"]
    assert "eyJ" not in err and "token=***" in err


def test_extension_temporal_segun_la_ruta(monkeypatch):
    vistas = []
    monkeypatch.setattr(gv, "download", lambda url, dest: vistas.append(os.path.basename(dest)) or 1)
    monkeypatch.setattr(gv, "decode_file", lambda f: (_ for _ in ()).throw(RuntimeError("ffmpeg_failed:x")))
    for ruta in ("a/b.MP3", "a/b.m4a", None):
        monkeypatch.setattr(gv, "api", Plataforma({**TRABAJO, "track": {**TEMA, "path": ruta}}).api)
        gv.process_one()
    assert vistas == ["in.mp3", "in.m4a", "in.m4a"]


def test_si_el_reporte_falla_no_se_cae(monkeypatch, capsys):
    monkeypatch.setattr(gv, "api", Plataforma({**TRABAJO, "audio_url": None}, falla_reporte=True).api)
    assert gv.process_one() is True
    msgs = [r["msg"] for r in registros(capsys)]
    assert msgs[-2:] == ["failed", "report_failed"]


# ── main_loop ────────────────────────────────────────────────────────────────
def test_main_loop_sin_variables_termina(monkeypatch):
    monkeypatch.setattr(gv, "filtrar_salida", lambda: None)
    monkeypatch.setattr(gv, "API", "")
    with pytest.raises(SystemExit) as e:
        gv.main_loop()
    assert e.value.code == 1


def test_main_loop_espera_tras_un_error_y_para_con_la_senal(monkeypatch, capsys):
    monkeypatch.setattr(gv, "filtrar_salida", lambda: None)
    monkeypatch.setattr(gv, "API", "http://plataforma.invalid")
    monkeypatch.setattr(gv, "SECRET", "s3")
    monkeypatch.setattr(gv, "POLL_S", 5)
    monkeypatch.setattr(gv.random, "uniform", lambda a, b: 1.0)
    monkeypatch.setattr(gv, "_stop", False)
    vueltas = iter([True, RuntimeError("red caída"), False])

    def process_one():
        v = next(vueltas)
        if isinstance(v, Exception):
            raise v
        return v

    dormidas = []

    def sleep(s):
        dormidas.append(s)
        if len(dormidas) == 5 + 10:  # 5 s tras el error y 10 s con la cola vacía
            gv._on_signal()

    monkeypatch.setattr(gv, "process_one", process_one)
    monkeypatch.setattr(gv.time, "sleep", sleep)
    monkeypatch.setattr(gv.signal, "signal", lambda *a: None)  # no dejar manejadores puestos
    gv.main_loop()
    assert len(dormidas) == 15
    msgs = [r["msg"] for r in registros(capsys)]
    assert msgs[0] == "start" and "loop_error" in msgs and msgs[-1] == "stop"
    assert math.isclose(sum(dormidas), 15)
