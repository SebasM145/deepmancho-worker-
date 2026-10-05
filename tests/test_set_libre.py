"""Render de sets: el método sin plan, el máster, la subida y la cola (W6, relevo de noche 5-oct).

`_mezcla_libre` (sets viejos, sin `spec.transiciones`), `_masterizar_y_subir`,
la limpieza de `render_set` y `poll_set_render` no tenían prueba (test_set_plan
cubre el método con plan, #143). Sin red y sin ffmpeg: las descargas y la
decodificación se simulan como en test_set_plan, y el MP3 del máster se
reemplaza por una copia del WAV para poder medirlo. Fijan el comportamiento actual.
"""
import os
import shutil
import sys

import numpy as np
import pytest
import soundfile as sf

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import worker  # noqa: E402

SR = worker.SET_SR
FIRMADA = "https://x.supabase.co/storage/v1/object/sign/music/a.mp3?token=eyJhbGciOiJIUzI1NiJ9.eyJ4Ijox.abc"


def ruido(dur_s, amp=0.1, semilla=0):
    """Estéreo sin graves (para que el bass-swap no cambie el nivel) y distinto por semilla."""
    x = np.random.RandomState(semilla).randn(int(dur_s * SR), 2).astype(np.float32) * amp
    return worker._low_shelf(x, 200.0, 0.0)


@pytest.fixture
def libre(monkeypatch, tmp_path):
    """_mezcla_libre con descargas y decodificación simuladas; `_stretch` espía."""
    audios, estirados = {}, []

    class R:
        status_code = 200

        def __init__(self, url):
            self.content = url.encode()

    monkeypatch.setattr(worker.requests, "get", lambda url, timeout=None: R(url))
    monkeypatch.setattr(worker, "_decode_pcm", lambda p: audios[open(p, "rb").read().decode()].copy())

    def stretch(path, ratio, tmpdir):
        estirados.append(round(ratio, 6))
        return path

    monkeypatch.setattr(worker, "_stretch", stretch)

    def correr(*temas):
        tracks = []
        for i, (bpm, audio, extra) in enumerate(temas):
            url = f"http://audio/{i}"
            audios[url] = audio
            tracks.append({"id": f"t{i}", "title": f"Tema {i}", "artist": "A", "label": "L",
                           "bpm": bpm, "audio_url": url, **extra})
        return worker._mezcla_libre(tracks, str(tmp_path))

    correr.estirados = estirados
    return correr


def test_primer_tema_arranca_en_su_mix_in(libre):
    a = ruido(30)
    sal, tl = libre((120, a, {"cue_points": [{"label": "mix-in", "positionMs": 2000}]}))
    assert np.array_equal(sal, a[2 * SR:])
    assert tl == [{"position": 1, "start_seconds": 0, "track_id": "t0", "title": "Tema 0", "artist": "A", "label": "L"}]


def test_tema_sin_bpm_se_omite(libre, capsys):
    a, b = ruido(60, semilla=1), ruido(30, semilla=2)
    sal, tl = libre((120, a, {}), (None, ruido(10), {}), (120, b, {}))
    assert [t["track_id"] for t in tl] == ["t0", "t2"]
    assert tl[1]["position"] == 3
    assert "sin BPM, se omite" in capsys.readouterr().out


def test_cruce_de_12_compases_antes_del_mix_out(libre, capsys):
    # 120 BPM → compás de 2 s. Sin cues, el MIX-OUT es el 90 % (54 s de la saliente).
    # Hay 54 s: 16 compases (32 s) pasan de la mitad, así que baja a 12 (24 s) y empieza en 30 s.
    a, b = ruido(60, semilla=1), ruido(80, semilla=2)
    sal, tl = libre((120, a, {}), (120, b, {}))
    assert tl[1]["start_seconds"] == 30
    assert len(sal) == 30 * SR + len(b)
    assert "transicion 12 compases" in capsys.readouterr().out
    assert np.array_equal(sal[: 30 * SR], a[: 30 * SR])            # antes del cruce, la saliente intacta
    assert np.array_equal(sal[54 * SR:], b[24 * SR:])               # después, la entrante intacta


def test_cruce_corto_si_la_entrante_es_corta(libre, capsys):
    # El espacio también lo limita la entrante: 40 s → MIX-OUT a 36 s → mitad 18 s → 8 compases.
    a, b = ruido(60, semilla=1), ruido(40, semilla=2)
    sal, tl = libre((120, a, {}), (120, b, {}))
    assert "transicion 8 compases" in capsys.readouterr().out
    assert len(sal) == 38 * SR + len(b)


def test_cruce_equal_power_sin_hueco(libre):
    a, b = ruido(60, semilla=1), ruido(80, semilla=2)
    sal, _ = libre((120, a, {}), (120, b, {}))
    rms = lambda x: float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    medio = sal[42 * SR - SR // 2: 42 * SR + SR // 2]               # mitad del cruce (30 s + 12 s)
    # Dos ruidos independientes a cos/sin de 45°: el nivel queda igual (no cae 3 dB).
    assert rms(medio) == pytest.approx(rms(a), rel=0.08)


def test_entrante_cae_en_la_fase_del_compas(libre):
    # La entrante tiene su primer tiempo a 500 ms: fase (0 − 500) mod 2000 = 1500 ms,
    # así que el cruce empieza 1,5 s más tarde para que los compases coincidan.
    a, b = ruido(60, semilla=1), ruido(80, semilla=2)
    sal, tl = libre((120, a, {}), (120, b, {"first_beat_offset_ms": 500}))
    assert len(sal) == int(31.5 * SR) + len(b)
    assert tl[1]["start_seconds"] == 31


@pytest.mark.parametrize("bpm_b,estirados", [
    (124, []),                         # mismo tempo: nada que estirar
    (124.05, []),                      # 0,04 %: no vale la pena
    (130, [round(130 / 124, 6)]),      # 4,6 % < 6 %: se estira hasta el tempo del set
])
def test_estira_solo_lo_que_hace_falta(libre, capsys, bpm_b, estirados):
    libre((124, ruido(60, semilla=1), {}), (bpm_b, ruido(40, semilla=2), {}))
    assert libre.estirados == estirados
    assert "sin estirar" not in capsys.readouterr().out


def test_mas_del_6_por_ciento_no_se_fuerza(libre, capsys):
    libre((120, ruido(60, semilla=1), {}), (135, ruido(40, semilla=2), {}))
    assert libre.estirados == []
    assert "> 6.0% — sin estirar" in capsys.readouterr().out


def test_cues_en_el_tiempo_del_tema_estirado(libre):
    # El segundo tema a 125 se estira a 120 (el estirado es simulado: no cambia el largo).
    # Sus cues se escalan por 120/125: MIX-IN 4,1 s → 3,936 s. La fase del compás queda en
    # 1936 ms, así que el cruce de 12 compases empieza en 54 − 24 + 1,936 = 31,936 s.
    # (Sin escalar, el MIX-IN en 4,1 s caería en otro compás y el set duraría 2 s menos).
    a, b = ruido(60, semilla=1), ruido(80, semilla=2)
    sal, _ = libre((120, a, {}), (125, b, {"cue_points": [{"label": "MIX-IN", "positionMs": 4100}]}))
    assert len(sal) == pytest.approx(int(31.936 * SR) + len(b) - int(3.936 * SR), abs=2)
    assert np.array_equal(sal[-SR:], b[-SR:])


# ─────────────────────────────── máster y subida ───────────────────────────────
@pytest.fixture
def master(monkeypatch, tmp_path):
    """El «MP3» es una copia del WAV (sin ffmpeg) y la subida guarda lo que recibió."""
    subido = {}

    def run(cmd, check=False, **k):
        assert cmd[0] == "ffmpeg" and "libmp3lame" in cmd and "256k" in cmd
        shutil.copy(cmd[cmd.index("-i") + 1], cmd[-1])

    class R:
        def __init__(self, ok):
            self.ok = ok

        def raise_for_status(self):
            if not self.ok:
                raise RuntimeError("HTTP 400")

    def put(url, data=None, headers=None, timeout=None):
        p = tmp_path / "subido.wav"
        p.write_bytes(data.read())
        subido.update(url=url, headers=headers, path=p)
        return R("falla" not in url)

    monkeypatch.setattr(worker.subprocess, "run", run)
    monkeypatch.setattr(worker.requests, "put", put)
    return subido


def lufs(path):
    import pyloudnorm as pyln
    x, sr = sf.read(str(path))
    return pyln.Meter(sr).integrated_loudness(x.mean(axis=1))


def test_master_lleva_el_set_a_menos_14_lufs(master, tmp_path):
    t = np.arange(20 * SR) / SR
    tono = (0.02 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)   # poco pico: el techo no actúa
    salida = np.stack([tono, tono], axis=1)
    dur = worker._masterizar_y_subir(salida, str(tmp_path), "https://x/up/set.mp3", "sets/s.mp3")
    assert dur == pytest.approx(20.0)
    assert master["url"] == "https://x/up/set.mp3" and master["headers"] == {"Content-Type": "audio/mpeg"}
    assert lufs(master["path"]) == pytest.approx(worker.SET_TARGET_LUFS, abs=0.3)


def test_master_nunca_pasa_el_techo_de_menos_1_db(master, tmp_path):
    salida = ruido(20, amp=0.1)
    salida[SR] = [0.99, -0.99]                         # un pico que, al subir el nivel, pasaría de 0 dB
    worker._masterizar_y_subir(salida * 0.2, str(tmp_path), "https://x/up", "r")
    x, _ = sf.read(str(master["path"]))
    assert np.max(np.abs(x)) <= 10 ** (-1 / 20) + 1e-4


def test_master_de_silencio_no_rompe(master, tmp_path):
    dur = worker._masterizar_y_subir(np.zeros((SR * 2, 2), np.float32), str(tmp_path), "https://x/up", "r")
    assert dur == pytest.approx(2.0)
    x, _ = sf.read(str(master["path"]))
    assert np.all(np.isfinite(x)) and np.max(np.abs(x)) == 0


def test_subida_que_falla_corta_el_set(master, tmp_path):
    with pytest.raises(RuntimeError, match="HTTP 400"):
        worker._masterizar_y_subir(ruido(2), str(tmp_path), "https://x/falla", "r")


# ─────────────────────────────── render_set y la cola ───────────────────────────────
def test_render_set_borra_su_carpeta_aunque_falle(monkeypatch, tmp_path):
    carpetas = []
    real = worker.tempfile.mkdtemp

    def mkdtemp(prefix=""):
        d = real(prefix=prefix, dir=str(tmp_path))
        carpetas.append(d)
        return d

    monkeypatch.setattr(worker.tempfile, "mkdtemp", mkdtemp)
    monkeypatch.setattr(worker, "_mezcla_libre", lambda tracks, d: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        worker.render_set({}, [{"id": "a"}], "u", "r")
    assert carpetas and not os.path.exists(carpetas[0])


def test_plan_que_no_coincide_usa_el_metodo_anterior(monkeypatch, capsys):
    usado = []
    monkeypatch.setattr(worker, "_mezcla_libre", lambda tracks, d: usado.append("libre") or (np.zeros((4, 2), np.float32), []))
    monkeypatch.setattr(worker, "_mezcla_plan", lambda *a: usado.append("plan"))
    monkeypatch.setattr(worker, "_masterizar_y_subir", lambda s, d, u, r: 1.0)
    plan = [{"desde": "x", "hasta": "y", "tipo": "corte"}]
    assert worker.render_set({"spec": {"transiciones": plan}}, [{"id": "a"}, {"id": "b"}], "u", "r") == (1.0, [])
    assert usado == ["libre"]
    assert "no coincide con los temas" in capsys.readouterr().out


@pytest.fixture
def cola(monkeypatch):
    """_set_api simulado: `respuestas[action]` es lo que devuelve (o la excepción que lanza)."""
    estado = {"respuestas": {"next": {"job": None}}, "pedidos": [], "render": (125.7, [{"position": 1}])}

    def api(action, payload=None):
        estado["pedidos"].append((action, payload))
        r = estado["respuestas"].get(action, {"ok": True})
        if isinstance(r, Exception):
            raise r
        return r

    def render_set(job, tracks, upload_url, result_path):
        estado["render_args"] = (job, tracks, upload_url, result_path)
        if isinstance(estado["render"], Exception):
            raise estado["render"]
        return estado["render"]

    monkeypatch.setattr(worker, "_set_api", api)
    monkeypatch.setattr(worker, "render_set", render_set)
    return estado


TRABAJO = {"job": {"id": "s1", "title": "Mi set"}, "tracks": [{"id": "a"}],
           "upload_url": "https://x/up/s1.mp3", "result_path": "sets/s1.mp3"}


def test_cola_caida_no_cuenta_como_trabajo(cola, capsys):
    cola["respuestas"]["next"] = RuntimeError("503")
    assert worker.poll_set_render() is False
    assert "[set-render] no disponible" in capsys.readouterr().out


def test_cola_vacia(cola):
    assert worker.poll_set_render() is False
    assert [a for a, _ in cola["pedidos"]] == ["next"]


def test_set_listo_se_entrega_sin_publicar(cola, capsys):
    cola["respuestas"]["next"] = TRABAJO
    assert worker.poll_set_render() is True
    assert cola["render_args"] == (TRABAJO["job"], [{"id": "a"}], "https://x/up/s1.mp3", "sets/s1.mp3")
    assert cola["pedidos"][1] == ("result", {"job_id": "s1", "result_path": "sets/s1.mp3",
                                             "duration_sec": 125, "tracklist": [{"position": 1}]})
    assert "SIN publicar" in capsys.readouterr().out


def test_set_que_falla_avisa_sin_firmas(cola):
    cola["respuestas"]["next"] = TRABAJO
    cola["render"] = RuntimeError(f"descarga de {FIRMADA} · 403 for url: /storage/v1/object/sign/b.mp3?token=eyJotro "
                                  + "z" * 3000)
    assert worker.poll_set_render() is True
    accion, cuerpo = cola["pedidos"][1]
    assert accion == "fail" and cuerpo["job_id"] == "s1"
    assert "eyJ" not in cuerpo["error"] and len(cuerpo["error"]) <= 2000


def test_set_que_falla_y_no_puede_avisar_no_tumba_el_worker(cola):
    cola["respuestas"]["next"] = TRABAJO
    cola["respuestas"]["fail"] = RuntimeError("503")
    cola["render"] = RuntimeError("sin memoria")
    assert worker.poll_set_render() is True
