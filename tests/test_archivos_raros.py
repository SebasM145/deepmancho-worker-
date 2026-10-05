"""«Todo por sistema» (5-oct-2026): ningún archivo raro termina en reintentos eternos. Cada uno
acaba rápido en `done` o en `error` con `determinista:` y un mensaje que el DJ entiende.
Los archivos se fabrican con ffmpeg (corre en CI); sin ffmpeg se saltan."""
import os
import shutil
import subprocess
import sys
import time

import numpy as np
import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import soundfile as sf  # noqa: E402
import worker  # noqa: E402

hay_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="sin ffmpeg")
SR = 44100


def _ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


@pytest.fixture(scope="module")
def taller(tmp_path_factory):
    d = tmp_path_factory.mktemp("raros")
    t = np.arange(40 * SR) / SR
    beat = 60 / 124
    x = np.sin(2 * np.pi * 55 * (t % beat)) * np.exp(-(t % beat) * 12) * 0.8 + 0.1 * np.sin(2 * np.pi * 440 * t)
    base = d / "base.wav"
    sf.write(base, np.stack([x, x], axis=1).astype(np.float32), SR, subtype="PCM_16")
    return d, base


def _armar(taller, nombre):
    d, base = taller
    p = d / nombre
    if nombre == "wav24_96.wav":
        _ff("-i", str(base), "-ar", "96000", "-c:a", "pcm_s24le", str(p))
    elif nombre == "tema.aiff":
        _ff("-i", str(base), "-c:a", "pcm_s16be", str(p))
    elif nombre == "tema.flac":
        _ff("-i", str(base), str(p))
    elif nombre == "cbr.mp3":
        _ff("-i", str(base), "-b:a", "320k", str(p))
    elif nombre == "vbr.mp3":
        _ff("-i", str(base), "-q:a", "9", str(p))
    elif nombre == "tema.m4a":
        _ff("-i", str(base), "-c:a", "aac", "-b:a", "192k", str(p))
    elif nombre == "portada_gigante.mp3":
        portada = d / "portada.png"
        _ff("-f", "lavfi", "-i", "nullsrc=s=3000x3000,geq=random(1)*255:128:128", "-frames:v", "1", str(portada))
        _ff("-i", str(base), "-i", str(portada), "-map", "0", "-map", "1", "-b:a", "320k",
            "-id3v2_version", "3", "-metadata:s:v", "comment=Cover (front)", str(p))
    elif nombre == "id3_roto.mp3":
        _ff("-i", str(base), "-b:a", "320k", "-write_id3v2", "0", str(d / "limpio.mp3"))
        p.write_bytes(b"ID3\x03\x00\x00\x7f\x7f\x7f\x7f" + (d / "limpio.mp3").read_bytes())
    elif nombre == "cortado.mp3":
        _ff("-i", str(base), "-b:a", "320k", str(d / "entero.mp3"))
        p.write_bytes((d / "entero.mp3").read_bytes()[:30_000])   # ~0,7 s y un cuadro a medias
    elif nombre == "basura.mp3":
        p.write_bytes(np.random.default_rng(1).integers(0, 256, 2_000_000, dtype=np.uint8).tobytes())
    elif nombre == "texto.wav":
        p.write_text("esto no es audio\n" * 1000)
    elif nombre == "vacio.mp3":
        p.write_bytes(b"")
    elif nombre == "imagen.mp3":
        _ff("-f", "lavfi", "-i", "testsrc=s=640x480", "-frames:v", "1", "-f", "image2", str(p))
    elif nombre == "solo_cabecera.wav":
        p.write_bytes(base.read_bytes()[:44])
    return p


def _procesar(monkeypatch, tmp_path, origen):
    copia = tmp_path / ("x" + origen.suffix)
    shutil.copy(origen, copia)
    enviados, analizados = [], []
    monkeypatch.setattr(worker, "download_audio", lambda url: str(copia))
    monkeypatch.setattr(worker, "send_result", lambda *a, **k: enviados.append((a[2], k)))
    real = worker.analyze
    monkeypatch.setattr(worker, "analyze", lambda path, bpm_seed=None: analizados.append(1) or real(path, bpm_seed))
    t0 = time.time()
    worker.process_job({"id": "j", "track_id": "t"}, {"artist": "a", "title": "b"}, "http://x/a" + origen.suffix)
    (estado, kw), = enviados
    return estado, kw, time.time() - t0, analizados


VALIDOS = ["wav24_96.wav", "tema.aiff", "tema.flac", "cbr.mp3", "vbr.mp3", "tema.m4a", "portada_gigante.mp3"]
INVALIDOS = ["basura.mp3", "texto.wav", "vacio.mp3", "imagen.mp3", "solo_cabecera.wav"]
DUDOSOS = ["id3_roto.mp3", "cortado.mp3"]      # según el decodificador: done o error claro, nunca otra cosa


@hay_ffmpeg
@pytest.mark.parametrize("nombre", VALIDOS)
def test_formatos_validos_se_analizan(monkeypatch, tmp_path, taller, nombre):
    estado, kw, seg, _ = _procesar(monkeypatch, tmp_path, _armar(taller, nombre))
    assert estado == "done", kw
    r = kw["result"]
    assert r["bpm"] and abs(r["bpm"] - 124) < 1.5 and r["loudness_lufs"] < 0
    assert seg < 90


@hay_ffmpeg
@pytest.mark.parametrize("nombre", INVALIDOS)
def test_archivos_ilegibles_terminan_con_mensaje_claro(monkeypatch, tmp_path, taller, nombre):
    estado, kw, seg, analizados = _procesar(monkeypatch, tmp_path, _armar(taller, nombre))
    assert estado == "error"
    assert kw["error"].startswith("determinista:")            # worker-result no reintenta
    assert kw["error"][len("determinista:"):] in (worker.MSJ_ILEGIBLE, worker.MSJ_MUY_CORTO)
    assert analizados == []                                   # se cortó antes de cargarlo
    assert seg < 10


@hay_ffmpeg
@pytest.mark.parametrize("nombre", DUDOSOS)
def test_archivos_dudosos_nunca_quedan_en_el_aire(monkeypatch, tmp_path, taller, nombre):
    estado, kw, seg, _ = _procesar(monkeypatch, tmp_path, _armar(taller, nombre))
    assert (estado == "done") or kw["error"].startswith("determinista:"), kw
    assert seg < 90


def test_tema_mas_largo_que_el_analisis_queda_marcado(monkeypatch, tmp_path):
    p = tmp_path / "a.mp3"
    p.write_bytes(b"x")
    enviados = []
    monkeypatch.setattr(worker, "download_audio", lambda url: str(p))
    monkeypatch.setattr(worker, "sondear_audio", lambda path: (3600.0, "mp3"))
    monkeypatch.setattr(worker, "analyze", lambda path, bpm_seed=None: {"energy": 7})
    monkeypatch.setattr(worker, "detectar_genero", lambda path: {})
    monkeypatch.setattr(worker, "medir_sonoridad", lambda path: {})
    monkeypatch.setattr(worker, "send_result", lambda *a, **k: enviados.append(k.get("result")))
    worker.process_job({"id": "j", "track_id": "t"}, {"artist": "a", "title": "b"}, "http://x/a.mp3")
    assert enviados[0]["analysis_flags"] == [f"analisis_parcial:primeros_{worker.MAX_DURATION}_s_de_3600"]


def test_sin_ffprobe_no_sondea(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda n: None)
    assert worker.sondear_audio("/no/existe") is None


def test_decodificacion_fallida_es_determinista(tmp_path):
    p = tmp_path / "x.mp3"
    p.write_bytes(b"\x00" * 10)
    with pytest.raises(worker.ArchivoIlegible):
        worker.analyze(str(p))
