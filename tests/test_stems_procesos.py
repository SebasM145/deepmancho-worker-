"""stems_worker: los procesos completos (W6, relevo de noche 5-oct).

`process` (separación + patrones), `process_loops`, `process_render`, el bucle
`main`, la descarga y la subida, `run_demucs`, la descarga de los pesos del
híbrido y `midi_notes` no tenían prueba. Sin torch, sin demucs y sin
basic-pitch, como el CI: la red se reemplaza por espías, demucs por un
proceso falso que deja WAV, basic-pitch por un módulo falso y `to_wav_mono`
por señales sintéticas de numpy. Las que codifican de verdad (MP3 de las
pistas, máster del render) usan ffmpeg y se saltan si no está instalado.
Fijan el comportamiento actual.
"""
import hashlib
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stems_worker as sw  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_stems_mapa_perfil import bateria  # noqa: E402  (bombo con clic y hats, la que lee drum_grid)

SR = 22050
FIRMADA = "https://x.supabase.co/storage/v1/object/sign/music/a.mp3?token=eyJhbGciOiJIUzI1NiJ9.eyJ4Ijox.abc"
con_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="sin ffmpeg")


def tono(hz, dur_s, sr=SR, amp=0.5):
    t = np.arange(int(dur_s * sr)) / sr
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def bombos(bpm, dur_s, sr=SR):
    """Un golpe grave y corto en cada tiempo, sobre un piso de ruido de −50 dB."""
    y = (np.random.RandomState(1).randn(int(dur_s * sr)) * 0.003).astype(np.float32)
    golpe = tono(60, 0.08, sr, 0.9) * np.exp(-np.arange(int(0.08 * sr)) / (0.02 * sr)).astype(np.float32)
    for k in range(int(dur_s * bpm / 60)):
        i = int(k * 60 / bpm * sr)
        y[i:i + len(golpe)] += golpe[: len(y) - i]
    return y


class Enviados(list):
    falla = False


@pytest.fixture
def bpm_global():
    """BPM_GLOBAL es una lista que `process` cambia: se deja como estaba."""
    antes = list(sw.BPM_GLOBAL)
    yield sw.BPM_GLOBAL
    sw.BPM_GLOBAL[:] = antes


class Respuesta:
    def __init__(self, ok=True, status=200, cuerpo=None, trozos=()):
        self.ok, self.status_code, self._cuerpo, self._trozos = ok, status, cuerpo or {}, trozos
        self.text = "error de prueba"

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._cuerpo

    def iter_content(self, _n):
        yield from self._trozos

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def posts(monkeypatch):
    """Espía de requests.post: guarda (ruta, cuerpo). `posts.falla` hace fallar la siguiente."""
    enviados = Enviados()

    def post(url, headers=None, json=None, timeout=None):
        enviados.append((url.rsplit("/", 1)[-1], json))
        if enviados.falla:
            return Respuesta(ok=False, status=500)
        return Respuesta(cuerpo={"ok": True})

    monkeypatch.setattr(sw.requests, "post", post)
    return enviados


@pytest.fixture
def temporal(monkeypatch, tmp_path):
    """mkdtemp dentro de tmp_path, para comprobar que el proceso lo borra al terminar."""
    hechos = []

    def mkdtemp(prefix=""):
        d = tmp_path / f"{prefix}{len(hechos)}"
        d.mkdir()
        hechos.append(d)
        return str(d)

    monkeypatch.setattr(sw.tempfile, "mkdtemp", mkdtemp)
    return hechos


# ─────────────────────────────── descarga y subida ───────────────────────────────
def test_download_guarda_el_archivo(monkeypatch, tmp_path):
    monkeypatch.setattr(sw.requests, "get", lambda url, stream, timeout: Respuesta(trozos=[b"ab", b"cd"]))
    dest = sw.download("http://x/a.mp3", tmp_path / "a.mp3")
    assert dest.read_bytes() == b"abcd"


def test_download_corta_lo_que_pasa_del_tope(monkeypatch, tmp_path):
    monkeypatch.setattr(sw, "MAX_MB", 1)
    mega = b"x" * (1 << 20)
    monkeypatch.setattr(sw.requests, "get", lambda url, stream, timeout: Respuesta(trozos=[mega, b"y"]))
    with pytest.raises(RuntimeError, match="mayor a 1 MB"):
        sw.download("http://x/a.mp3", tmp_path / "a.mp3")


def test_download_con_error_http_no_sigue(monkeypatch, tmp_path):
    monkeypatch.setattr(sw.requests, "get", lambda url, stream, timeout: Respuesta(ok=False, status=404))
    with pytest.raises(RuntimeError, match="404"):
        sw.download("http://x/a.mp3", tmp_path / "a.mp3")
    assert not (tmp_path / "a.mp3").exists()


@pytest.fixture
def puts(monkeypatch):
    hechos = []

    def put(url, data=None, headers=None, timeout=None):
        hechos.append({"url": url, "headers": headers, "datos": data.read()})
        return Respuesta(ok="falla" not in url, status=403)

    monkeypatch.setattr(sw.requests, "put", put)
    return hechos


def test_upload_con_token_aparte_lo_manda_en_la_cabecera(puts, tmp_path):
    p = tmp_path / "bass.mp3"
    p.write_bytes(b"mp3")
    sw.upload({"path": "u/bass.mp3", "url": "https://x/upload/sign/u/bass.mp3", "token": "tk"}, p)
    h = puts[0]["headers"]
    assert h["Authorization"] == "Bearer tk"
    assert h["x-upsert"] == "true" and h["Content-Type"] == "audio/mpeg"
    assert puts[0]["datos"] == b"mp3"


def test_upload_sin_cabecera_si_el_token_va_en_la_url_o_es_una_url_simple(puts, tmp_path):
    p = tmp_path / "bass.mp3"
    p.write_bytes(b"mp3")
    sw.upload({"path": "u/b.mp3", "url": "https://x/u/b.mp3?token=tk", "token": "tk"}, p)
    sw.upload("https://x/u/b.mp3", p, "audio/wav")
    assert "Authorization" not in puts[0]["headers"]
    assert "Authorization" not in puts[1]["headers"]
    assert puts[1]["headers"]["Content-Type"] == "audio/wav"


def test_upload_que_falla_corta_el_trabajo(puts, tmp_path):
    p = tmp_path / "bass.mp3"
    p.write_bytes(b"mp3")
    with pytest.raises(RuntimeError, match="subida falló 403"):
        sw.upload("https://x/falla.mp3", p)


# ─────────────────────────────── pesos del híbrido ───────────────────────────────
def test_bajar_con_sha_distinto_no_deja_nada(monkeypatch, tmp_path):
    monkeypatch.setattr(sw.requests, "get", lambda url, stream, timeout: Respuesta(trozos=[b"pesos"]))
    dest = tmp_path / "m.ckpt"
    with pytest.raises(RuntimeError, match="sha256 distinto"):
        sw._bajar("http://x/m.ckpt", dest, "0" * 64)
    assert list(tmp_path.iterdir()) == []


def test_bajar_con_sha_correcto_deja_el_archivo_completo(monkeypatch, tmp_path):
    monkeypatch.setattr(sw.requests, "get", lambda url, stream, timeout: Respuesta(trozos=[b"pe", b"sos"]))
    dest = tmp_path / "m.ckpt"
    sw._bajar("http://x/m.ckpt", dest, hashlib.sha256(b"pesos").hexdigest())
    assert dest.read_bytes() == b"pesos"
    assert [p.name for p in tmp_path.iterdir()] == ["m.ckpt"]


def test_asegurar_hibrido_baja_una_sola_vez(monkeypatch, tmp_path):
    monkeypatch.setenv("HIBRIDO_DIR", str(tmp_path))
    monkeypatch.setattr(sw, "MSST_CODIGO", {"models/scnet/scnet.py": "a" * 64,
                                            "models/bs_roformer/mel_band_roformer.py": "c" * 64})
    monkeypatch.setattr(sw, "HIBRIDO_PESOS", {"m.ckpt": ("https://x/m.ckpt", 5, "b" * 64)})
    bajados = []

    def bajar(url, dest, sha, timeout=600):
        bajados.append(url)
        dest.write_bytes(b"12345")

    monkeypatch.setattr(sw, "_bajar", bajar)
    base = sw.asegurar_hibrido()
    assert base == tmp_path
    assert len(bajados) == 3 and sw.MSST_COMMIT in bajados[0]
    for pkg in ("models", "models/bs_roformer", "models/scnet"):
        assert (tmp_path / "msst" / pkg / "__init__.py").exists()
    sw.asegurar_hibrido()
    assert len(bajados) == 3                       # ya estaba: no se vuelve a bajar
    (tmp_path / "m.ckpt").write_bytes(b"123")     # a medias (otro tamaño): se baja de nuevo
    sw.asegurar_hibrido()
    assert bajados[3:] == ["https://x/m.ckpt"]


# ─────────────────────────────── run_demucs ───────────────────────────────
@pytest.fixture
def demucs_falso(monkeypatch):
    """Reemplaza solo la llamada a demucs: deja WAV donde los dejaría el de verdad
    (<salida>/<modelo>/<nombre>/). ffmpeg sigue siendo el real."""
    real = subprocess.run
    estado = {"rc": 0, "carpeta": None, "pistas": ("drums", "bass", "other", "vocals"), "cmd": None}

    def run(cmd, *a, **k):
        if cmd[1:3] != ["-m", "demucs"]:
            return real(cmd, *a, **k)
        estado["cmd"] = cmd
        salida, src = Path(cmd[cmd.index("-o") + 1]), Path(cmd[-1])
        carpeta = salida / (estado["carpeta"] or cmd[cmd.index("-n") + 1]) / src.stem
        carpeta.mkdir(parents=True)
        for i, nombre in enumerate(estado["pistas"]):
            sf.write(str(carpeta / f"{nombre}.wav"), tono(110 * (i + 1), 0.5, 44100, 0.3), 44100)
        return subprocess.CompletedProcess(cmd, estado["rc"], "", "CUDA no disponible\nfalló el modelo")

    monkeypatch.setattr(sw.subprocess, "run", run)
    return estado


@con_ffmpeg
def test_run_demucs_codifica_mp3_y_borra_los_wav(demucs_falso, tmp_path):
    src = tmp_path / "input.mp3"
    otro = next(m for m in sorted(sw.DEMUCS_MODELOS) if m != sw.MODEL)   # no el de por defecto
    stems = sw.run_demucs(src, tmp_path / "out", otro)
    assert sorted(stems) == ["bass", "drums", "other", "vocals"]
    carpeta = tmp_path / "out" / otro / "input"            # la del modelo pedido (bug de la v1.9)
    assert all(p.parent == carpeta and p.suffix == ".mp3" and p.stat().st_size > 0 for p in stems.values())
    assert list(carpeta.glob("*.wav")) == []
    cmd = demucs_falso["cmd"]
    assert cmd[cmd.index("-n") + 1] == otro and "--mp3" not in cmd


def test_run_demucs_que_falla_trae_el_final_del_error(demucs_falso, tmp_path):
    demucs_falso["rc"] = 1
    with pytest.raises(RuntimeError, match="(?s)demucs salió con 1: .*falló el modelo"):
        sw.run_demucs(tmp_path / "input.mp3", tmp_path / "out", "htdemucs")


def test_run_demucs_sin_pistas_dice_donde_hay(demucs_falso, tmp_path):
    demucs_falso["pistas"] = ()
    otra = tmp_path / "out" / "htdemucs" / "input"
    otra.mkdir(parents=True)
    (otra / "bass.mp3").write_bytes(b"x")
    with pytest.raises(RuntimeError, match=r"no produjo stems en htdemucs_6s/input; sí hay: \['htdemucs/input/bass.mp3'\]"):
        sw.run_demucs(tmp_path / "input.mp3", tmp_path / "out", "htdemucs_6s")


# ─────────────────────────────── midi_notes ───────────────────────────────
@pytest.fixture
def basic_pitch(monkeypatch, bpm_global):
    eventos = []
    paquete = types.ModuleType("basic_pitch")
    paquete.ICASSP_2022_MODEL_PATH = "modelo"
    inferencia = types.ModuleType("basic_pitch.inference")
    inferencia.predict = lambda path, modelo, **k: (None, None, list(eventos))
    monkeypatch.setitem(sys.modules, "basic_pitch", paquete)
    monkeypatch.setitem(sys.modules, "basic_pitch.inference", inferencia)
    bpm_global[0] = 120.0
    return eventos


def test_midi_notes_cuantiza_al_cuarto_de_tiempo_desde_el_ancla(basic_pitch):
    # 120 BPM, ancla en 500 ms: el tiempo 0 está en 0,5 s y cada tiempo dura 0,5 s.
    basic_pitch += [
        (1.13, 1.16, 45, 0.51, []),   # 1,26 → 1,25; dura 0,06 tiempos → mínimo 0,25
        (1.00, 1.50, 40, 0.8, []),    # tiempo 1, dura 1
        (1.00, 1.25, 38, 0.7, []),    # mismo tiempo: ordena por nota
        (1.00, 1.50, 70, 0.9, []),    # fuera del rango del bajo
        (0.20, 0.40, 40, 0.9, []),    # antes del ancla
    ]
    _, to_beat = sw.beat_grid(120.0, 500.0)
    notas = sw.midi_notes(Path("bass.mp3"), to_beat, 24, 60)
    assert notas == [
        {"b": 1.0, "d": 0.5, "n": 38, "v": 0.7},
        {"b": 1.0, "d": 1.0, "n": 40, "v": 0.8},
        {"b": 1.25, "d": 0.25, "n": 45, "v": 0.51},
    ]


def test_midi_notes_respeta_el_tope(basic_pitch):
    basic_pitch += [(1.0 + k * 0.5, 1.2 + k * 0.5, 40, 0.5, []) for k in range(10)]
    _, to_beat = sw.beat_grid(120.0, 0.0)
    assert [n["b"] for n in sw.midi_notes(Path("b.mp3"), to_beat, 24, 60, max_notes=3)] == [2.0, 3.0, 4.0]


# ─────────────────────────────── process (separación) ───────────────────────────────
BPM = 120.0
DUR = 32.0


@pytest.fixture
def separacion(monkeypatch, posts, temporal, bpm_global):
    """Un trabajo de separación con todo lo de afuera simulado. Los patrones se
    calculan de verdad sobre señales sintéticas."""
    senales = {
        "drums": bateria(BPM, 16)[: int(DUR * SR)],
        "bass": tono(55, DUR, amp=0.4),
        "other": tono(440, DUR, amp=0.2) + tono(554, DUR, amp=0.2),
        "piano": tono(262, DUR, amp=0.3),
        "vocals": tono(330, DUR, amp=0.2),
    }
    estado = {"motor": {"engine": "demucs", "model": "htdemucs_6s", "engine_version": "demucs 4.0.1 · htdemucs_6s"},
              "subidas": [], "midi_falla": set(), "separar": None}

    def to_wav_mono(path, sr=22050):
        y = senales[Path(path).stem]
        return (y[:: SR // sr] if sr != SR else y), sr

    def separar(src, outdir, job=None):
        if estado["separar"]:
            raise estado["separar"]
        return {k: outdir / f"{k}.mp3" for k in senales}, dict(estado["motor"])

    def midi_notes(path, to_beat, lo, hi, max_notes=4000):
        nombre = Path(path).stem
        if nombre in estado["midi_falla"]:
            raise RuntimeError("basic-pitch se cayó")
        base = {"bass": 33, "other": 69, "piano": 60, "vocals": 64, "guitar": 52}[nombre]
        return [{"b": float(b), "d": 1.0, "n": base + (b % 3), "v": 0.6} for b in range(0, 64, 2)]

    monkeypatch.setattr(sw, "download", lambda url, dest: dest)
    monkeypatch.setattr(sw, "ffprobe_duration", lambda p: DUR)
    monkeypatch.setattr(sw, "to_wav_mono", to_wav_mono)
    monkeypatch.setattr(sw, "separar", separar)
    monkeypatch.setattr(sw, "midi_notes", midi_notes)
    monkeypatch.setattr(sw, "lufs_of", lambda p: -12.5)
    monkeypatch.setattr(sw, "upload", lambda target, p, content_type="audio/mpeg": estado["subidas"].append((target["path"], p.name)))
    bpm_global[0] = 126.0
    return estado


def trabajo(**extra):
    j = {"job_id": "11111111-2222", "audio_url": FIRMADA, "bpm": 119, "bpm_fine": 1.0,
         "first_beat_offset_ms": 0, "duration_seconds": DUR,
         "uploads": {k: {"path": f"u/t/{k}.mp3", "url": f"https://x/{k}", "token": "tk"} for k in ("drums", "bass", "other", "vocals")}}
    j.update(extra)
    return j


def test_process_entrega_pistas_y_patrones(separacion, posts, temporal):
    sw.process(trabajo())
    assert sw.BPM_GLOBAL[0] == 120.0                      # bpm + bpm_fine, no bpm_fine solo
    # Solo se suben las pistas que el trabajo pidió (piano no tiene destino).
    assert sorted(separacion["subidas"]) == [(f"u/t/{k}.mp3", f"{k}.mp3") for k in ("bass", "drums", "other", "vocals")]
    assert len(posts) == 1
    ruta, cuerpo = posts[0]
    assert ruta == "stems-result" and cuerpo["ok"] is True and cuerpo["error"] is None
    assert cuerpo["stems"]["bass"] == {"path": "u/t/bass.mp3", "duration_seconds": DUR, "lufs": -12.5}
    assert "piano" not in cuerpo["stems"]
    pat = cuerpo["patterns"]
    assert set(pat) == {"bass_midi", "melody_midi", "drum_grid", "chords", "stats", "model", "sound_profile",
                        "parts", "blocks", "arrangement_map", "stem_quality", "sampler", "engine", "engine_version"}
    assert pat["model"] == "htdemucs_6s" and pat["engine"] == "demucs"
    assert pat["melody_midi"][0]["n"] == 60               # la melodía sale del piano antes que de other
    assert pat["parts"]["bass"] == pat["bass_midi"]
    assert set(pat["parts"]) == {"bass", "other", "piano", "vocals"}
    assert pat["drum_grid"]["bars"] == 16 and any(map(any, pat["drum_grid"]["kick"]))
    assert len(pat["chords"]) == 16
    assert set(pat["arrangement_map"]["instrumentos"]) >= {"bass", "drums", "other"}
    assert "vocals" not in pat["sampler"]                  # una voz no se clona por notas
    assert not temporal[0].exists()                        # la carpeta temporal se borra


def test_process_anota_cuando_el_hibrido_cayo_en_demucs(separacion, posts):
    separacion["motor"]["engine_fallback"] = "hibrido falló: MemoryError()"
    sw.process(trabajo())
    assert posts[0][1]["patterns"]["engine_fallback"] == "hibrido falló: MemoryError()"


def test_process_sigue_si_falla_la_transcripcion_o_el_mapa(separacion, posts, monkeypatch):
    separacion["midi_falla"].add("other")
    monkeypatch.setattr(sw, "mapa_arreglo", lambda *a: 1 / 0)
    monkeypatch.setattr(sw, "calidad_pistas", lambda *a: 1 / 0)
    sw.process(trabajo())
    cuerpo = posts[0][1]
    assert cuerpo["ok"] is True
    assert "other" not in cuerpo["patterns"]["parts"] and "piano" in cuerpo["patterns"]["parts"]
    assert cuerpo["patterns"]["arrangement_map"] is None and cuerpo["patterns"]["stem_quality"] is None


def test_process_sin_duracion_la_mide(separacion, posts, monkeypatch):
    medidas = []
    monkeypatch.setattr(sw, "ffprobe_duration", lambda p: medidas.append(p.name) or DUR)
    sw.process(trabajo(duration_seconds=None))
    assert medidas[0] == "input.mp3"


@pytest.mark.parametrize("bpm,fino", [(0, 0.001), (None, None), (250, 0), (59, 0.5)])
def test_process_sin_tempo_no_baja_nada(separacion, posts, temporal, monkeypatch, bpm, fino):
    monkeypatch.setattr(sw, "download", lambda *a: pytest.fail("no debía bajar el audio"))
    with pytest.raises(RuntimeError, match="tempo fuera de rango"):
        sw.process(trabajo(bpm=bpm, bpm_fine=fino))
    assert posts == [] and temporal == []


def test_process_que_falla_avisa_sin_la_url_firmada(separacion, posts, temporal):
    separacion["separar"] = RuntimeError(f"no se pudo leer {FIRMADA} " + "x" * 600)
    sw.process(trabajo())
    ruta, cuerpo = posts[0]
    assert ruta == "stems-result" and cuerpo["ok"] is False
    assert "eyJhbGciOiJIUzI1NiJ9" not in cuerpo["error"] and "token=" in cuerpo["error"]
    assert len(cuerpo["error"]) <= 520
    assert cuerpo["stems"] == {} and cuerpo["patterns"] == {}
    assert not temporal[0].exists()


def test_process_no_revienta_si_tampoco_puede_avisar(separacion, posts, temporal):
    separacion["separar"] = RuntimeError("sin memoria")
    posts.falla = True
    sw.process(trabajo())                                  # no lanza
    assert posts[0][1]["ok"] is False
    assert not temporal[0].exists()


# ─────────────────────────────── process_loops ───────────────────────────────
@pytest.fixture
def loops(monkeypatch, posts, temporal):
    estado = {"bajados": [], "cortar": None, "args": None}

    def download(url, dest):
        estado["bajados"].append((url, dest.name))
        return dest

    def cortar(track, lanes, energia, calidad, audio):
        estado["args"] = (track, lanes, energia, calidad, audio)
        if estado["cortar"]:
            raise estado["cortar"]
        return [{"stem": "bass", "role": "bajo", "from_bar": 9, "bars": 8, "qc": {}}], {"pista no limpia": 1}

    monkeypatch.setattr(sw, "download", download)
    monkeypatch.setattr(sw, "to_wav_mono", lambda p, sr=22050: (np.zeros(10, dtype=np.float32), sr))
    monkeypatch.setattr(sw, "cortar_loops", cortar)
    return estado


def pedido_loops(**extra):
    d = {"job": {"id": "33333333-4444"}, "track": {"bpm": 124, "phase_ms": 10, "genre": "house"},
         "stems": {"bass": "https://x/bass.mp3", "drums": "https://x/drums.mp3"}}
    d.update(extra)
    return d


def test_process_loops_baja_las_pistas_y_entrega(loops, posts, temporal):
    sw.process_loops(pedido_loops())
    assert sorted(loops["bajados"]) == [("https://x/bass.mp3", "bass.audio"), ("https://x/drums.mp3", "drums.audio")]
    track, lanes, energia, calidad, audio = loops["args"]
    assert track["genre"] == "house" and lanes == {} and energia == {} and calidad == {}
    assert sorted(audio) == ["bass", "drums"] and audio["bass"][1] == 22050
    assert posts == [("loops-result", {"job_id": "33333333-4444", "ok": True,
                                       "loops": [{"stem": "bass", "role": "bajo", "from_bar": 9, "bars": 8, "qc": {}}]})]
    assert not temporal[0].exists()


def test_process_loops_que_falla_avisa_sin_firma(loops, posts, temporal):
    loops["cortar"] = ValueError(f"no abre {FIRMADA} " + "y" * 900)
    sw.process_loops(pedido_loops())
    ruta, cuerpo = posts[0]
    assert ruta == "loops-result" and cuerpo["ok"] is False and cuerpo["loops"] == []
    assert cuerpo["error"].startswith("ValueError(") and "eyJ" not in cuerpo["error"] and len(cuerpo["error"]) == 500
    assert not temporal[0].exists()


def test_process_loops_no_revienta_si_tampoco_puede_avisar(loops, posts):
    loops["cortar"] = ValueError("x")
    posts.falla = True
    sw.process_loops(pedido_loops())
    assert posts[0][1]["ok"] is False


# ─────────────────────────────── process_render ───────────────────────────────
BPM_R = 120.0


def ficha(objetivo=-12.0, techo=-1.0):
    return {"bpm": BPM_R, "compases": 4, "fase_ms": 0,
            "master": {"objetivo_db": objetivo, "techo_db": techo},
            "pistas": [{"clave": "bombo", "desde_compas": 1, "respiro": False},
                       {"clave": "pad", "desde_compas": 2, "estereo": True, "espacio_db": -12, "ganancia_db": -6}]}


def pedido_render(**ficha_kw):
    return {"job": {"id": "55555555-6666"}, "ficha": ficha(**ficha_kw),
            "urls": {"bombo": "https://x/bombo", "pad": "https://x/pad"},
            "subir": {"wav": "https://x/subir/tema.wav", "mp3": "https://x/subir/tema.mp3"}}


@pytest.fixture
def render(monkeypatch, posts, temporal):
    senales = {"bombo": bombos(BPM_R, 6.0, sw.SR_RENDER) * 0.5,
               "pad": np.stack([tono(220, 4.0, sw.SR_RENDER, 0.1), tono(330, 4.0, sw.SR_RENDER, 0.1)])}
    estado = {"subidas": []}

    def download(url, dest):
        y = senales[url.rsplit("/", 1)[-1]]
        sf.write(str(dest.with_suffix(".wav")), y.T if y.ndim == 2 else y, sw.SR_RENDER)
        dest.with_suffix(".wav").rename(dest)
        return dest

    def subir(url, path, tipo):
        estado["subidas"].append((url.rsplit("/", 1)[-1], tipo, path.stat().st_size))

    monkeypatch.setattr(sw, "download", download)
    monkeypatch.setattr(sw, "subir", subir)
    estado["senales"] = senales
    return estado


@con_ffmpeg
def test_process_render_masteriza_al_objetivo_sin_pasar_el_techo(render, posts, temporal):
    sw.process_render(pedido_render(objetivo=-12.0, techo=-1.0))
    ruta, cuerpo = posts[0]
    assert ruta == "render-result" and cuerpo["ok"] is True, cuerpo
    m = cuerpo["medidas"]
    assert abs(m["lufs"] - (-12.0)) <= 0.5
    assert m["pico_db"] <= -1.0 + 0.01
    # Hasta el inicio del compás 5 (4 compases de 2 s) más 4 s de cola.
    assert m["duracion_s"] == pytest.approx(4 * 2.0 + 4, abs=0.01)
    assert [(n, t) for n, t, _ in render["subidas"]] == [("tema.wav", "audio/wav"), ("tema.mp3", "audio/mpeg")]
    assert all(tam > 1000 for _, _, tam in render["subidas"])
    assert not temporal[0].exists()


def medidor_falso(monkeypatch, desvio_db):
    """codificar guarda el máster en memoria y lufs_of lo mide con la aproximación
    del render más un desvío fijo (como si el medidor real no coincidiera)."""
    guardado = {}
    monkeypatch.setattr(sw, "to_wav_estereo", lambda p: np.asarray(sf.read(str(p), always_2d=True)[0].T, dtype=np.float32))
    monkeypatch.setattr(sw, "to_wav_mono", lambda p, sr=22050: (np.asarray(sf.read(str(p))[0], dtype=np.float32), sr))
    def codificar(st, dest, fmt):
        guardado[dest.name] = st.copy()
        dest.write_bytes(b"x" * 2000)

    monkeypatch.setattr(sw, "codificar", codificar)
    medidas = []

    def lufs_of(path):
        v = sw.sonoridad_aprox_db(guardado[path.name]) + desvio_db
        medidas.append(round(v, 2))
        return v

    monkeypatch.setattr(sw, "lufs_of", lufs_of)
    return medidas


def test_process_render_acumula_la_correccion_sobre_la_mezcla_original(render, posts, monkeypatch):
    # El medidor real da 2 dB menos que la aproximación: hacen falta varias vueltas.
    # Si cada vuelta corrigiera sobre la señal ya limitada (bug de la v1.22.1), no llegaría.
    medidas = medidor_falso(monkeypatch, -2.0)
    sw.process_render(pedido_render(objetivo=-14.0, techo=-1.0))
    cuerpo = posts[0][1]
    assert cuerpo["ok"] is True
    assert len(medidas) >= 3                               # al menos una corrección
    assert abs(cuerpo["medidas"]["lufs"] - (-14.0)) <= 0.5
    assert cuerpo["medidas"]["pico_db"] <= -1.0 + 0.01


def test_process_render_no_sube_mas_de_24_db(render, posts, monkeypatch, capsys):
    render["senales"]["bombo"] = render["senales"]["bombo"] * 1e-4
    render["senales"]["pad"] = render["senales"]["pad"] * 1e-4
    medidor_falso(monkeypatch, -30.0)                      # el medidor nunca llega al objetivo
    sw.process_render(pedido_render(objetivo=-9.0))
    assert posts[0][1]["ok"] is True
    assert "más de 24 dB" in capsys.readouterr().out


def test_process_render_que_falla_avisa_sin_firma(render, posts, temporal, monkeypatch):
    monkeypatch.setattr(sw, "download", lambda url, dest: (_ for _ in ()).throw(RuntimeError(f"403 en {FIRMADA}")))
    sw.process_render(pedido_render())
    ruta, cuerpo = posts[0]
    assert ruta == "render-result" and cuerpo["ok"] is False and "medidas" not in cuerpo
    assert "eyJ" not in cuerpo["error"] and "403" in cuerpo["error"]
    assert render["subidas"] == [] and not temporal[0].exists()


def test_process_render_no_revienta_si_tampoco_puede_avisar(render, posts, monkeypatch):
    monkeypatch.setattr(sw, "download", lambda url, dest: 1 / 0)
    posts.falla = True
    sw.process_render(pedido_render())
    assert posts[0][1]["ok"] is False


# ─────────────────────────────── main (el bucle) ───────────────────────────────
class Parar(BaseException):
    pass


@pytest.fixture
def bucle(monkeypatch):
    """Colas guionadas: cada claim devuelve el siguiente de su lista (None al final).
    `time.sleep` corta el bucle la primera vez que la espera es la de cola vacía."""
    colas = {"render": [], "stems": [], "loops": []}
    hechos = []
    monkeypatch.setattr(sw, "filtrar_salida", lambda: None)
    monkeypatch.setattr(sw.signal, "signal", lambda *a: None)
    monkeypatch.setattr(sw, "claim_render", lambda: colas["render"].pop(0) if colas["render"] else None)
    monkeypatch.setattr(sw, "claim", lambda: colas["stems"].pop(0) if colas["stems"] else None)
    monkeypatch.setattr(sw, "claim_loops", lambda: colas["loops"].pop(0) if colas["loops"] else None)
    monkeypatch.setattr(sw, "process_render", lambda d: hechos.append(("render", d)))
    monkeypatch.setattr(sw, "process_loops", lambda d: hechos.append(("loops", d)))

    def process(job):
        hechos.append(("stems", job["job_id"], sw.CURRENT_JOB["id"]))
        if job.get("revienta"):
            raise RuntimeError("tempo fuera de rango")

    monkeypatch.setattr(sw, "process", process)
    esperas = []

    def sleep(s):
        esperas.append(s)
        if len(esperas) >= colas.get("max_esperas", 1):
            raise Parar

    monkeypatch.setattr(sw.time, "sleep", sleep)
    return colas, hechos, esperas


def test_main_render_primero_y_un_loop_despues_de_cada_separacion(bucle):
    colas, hechos, _ = bucle
    colas["render"] = ["R1"]
    colas["stems"] = [{"job_id": "S1"}, {"job_id": "S2"}]
    colas["loops"] = ["L1", None, "L2"]
    with pytest.raises(Parar):
        sw.main()
    assert hechos == [("render", "R1"), ("stems", "S1", "S1"), ("loops", "L1"),
                      ("stems", "S2", "S2"), ("loops", "L2")]
    assert sw.CURRENT_JOB["id"] is None


def test_main_una_separacion_que_revienta_no_tumba_el_worker(bucle):
    colas, hechos, esperas = bucle
    colas["stems"] = [{"job_id": "S1", "revienta": True}, {"job_id": "S2"}]
    colas["max_esperas"] = 2
    with pytest.raises(Parar):
        sw.main()
    # La excepción la toma el bucle (espera de error) y el siguiente trabajo sí se procesa.
    assert [h[1] for h in hechos if h[0] == "stems"] == ["S1", "S2"]
    assert sw.CURRENT_JOB["id"] is None
    assert len(esperas) == 2
