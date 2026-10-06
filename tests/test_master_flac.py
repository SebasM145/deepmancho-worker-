"""Decisión de Germán (6-oct-2026): WAV/AIFF → master FLAC sin pérdida, verificado muestra por
muestra; MP3 y lo que no es PCM entero, tal cual. Archivos hechos con ffmpeg (corre en CI)."""
import os
import shutil
import subprocess
import sys

import numpy as np
import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import soundfile as sf  # noqa: E402
import worker  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="sin ffmpeg")


def _ff(*a):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *a], check=True)


def _ruido(sr, seg=8, ch=2):
    rng = np.random.default_rng(7)
    return (rng.standard_normal((sr * seg, ch)) * 0.3).clip(-1, 1)


@pytest.mark.parametrize("ext,subtipo,sr", [("wav", "PCM_16", 44100), ("wav", "PCM_24", 48000),
                                            ("wav", "PCM_24", 96000), ("aiff", "PCM_16", 44100),
                                            ("aiff", "PCM_24", 48000)])
def test_wav_y_aiff_pasan_a_flac_identico(tmp_path, ext, subtipo, sr):
    src = tmp_path / f"o.{ext}"
    sf.write(src, _ruido(sr), sr, subtype=subtipo)
    assert worker.codec_de(str(src)) in worker.CODECS_PCM_ENTERO
    out = worker.make_master_flac(str(src))
    assert out and out.endswith(".flac")
    i = sf.info(out)
    assert (i.format, i.subtype, i.samplerate, i.channels) == ("FLAC", subtipo, sr, 2)   # misma profundidad y sr
    a, _ = sf.read(src, dtype="int32")
    b, _ = sf.read(out, dtype="int32")
    assert np.array_equal(a, b)                                                          # audio idéntico
    assert os.path.getsize(out) < os.path.getsize(src) * 1.05


def test_metadatos_y_portada_pasan_al_flac(tmp_path):
    base = tmp_path / "b.aiff"
    sf.write(base, _ruido(44100), 44100, subtype="PCM_24")
    portada = tmp_path / "p.png"
    _ff("-f", "lavfi", "-i", "testsrc=s=600x600", "-frames:v", "1", str(portada))
    src = tmp_path / "con_portada.aiff"
    _ff("-i", str(base), "-i", str(portada), "-map", "0", "-map", "1", "-c:a", "copy", "-c:v", "copy",
        "-write_id3v2", "1", "-id3v2_version", "3", "-metadata", "title=Samburu", "-metadata", "artist=Germán",
        "-metadata", "genre=Afro House", str(src))
    out = worker.make_master_flac(str(src))
    assert out
    info = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags:stream=codec_type,codec_name",
                           "-of", "default=nw=1", out], capture_output=True, text=True).stdout.lower()
    assert "samburu" in info and "afro house" in info
    assert "codec_type=video" in info                       # la portada viaja con el FLAC
    a, _ = sf.read(base, dtype="int32")
    b, _ = sf.read(out, dtype="int32")
    assert np.array_equal(a, b)


def test_mp3_no_es_pcm_y_queda_tal_cual(tmp_path):
    base = tmp_path / "b.wav"
    sf.write(base, _ruido(44100), 44100)
    mp3 = tmp_path / "t.mp3"
    _ff("-i", str(base), "-b:a", "320k", str(mp3))
    antes = mp3.read_bytes()
    assert worker.codec_de(str(mp3)) not in worker.CODECS_PCM_ENTERO
    assert mp3.read_bytes() == antes


def test_wav_flotante_queda_tal_cual(tmp_path):
    src = tmp_path / "f.wav"
    sf.write(src, _ruido(44100), 44100, subtype="FLOAT")
    assert worker.codec_de(str(src)) not in worker.CODECS_PCM_ENTERO


def test_mismo_audio_detecta_una_muestra_distinta(tmp_path):
    x = (_ruido(44100) * 32767).astype(np.int16)
    a, b = tmp_path / "a.wav", tmp_path / "b.flac"
    sf.write(a, x, 44100)
    y = x.copy()
    y[12345, 1] += 1
    sf.write(b, y, 44100)
    assert worker.mismo_audio(str(a), str(a)) and not worker.mismo_audio(str(a), str(b))
