"""Género detectado (2-oct): etiqueta del archivo → nombre de Beatport con confianza;
lo amplio, lo desconocido o lo vacío queda «por revisar»."""
import os
import shutil
import subprocess
import sys

import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import worker  # noqa: E402


@pytest.mark.parametrize("crudo,genero,conf", [
    ("Tech House", "Tech House", 0.95),
    ("tech  house", "Tech House", 0.95),
    ("Melodic House & Techno", "Melodic House & Techno", 0.95),
    ("Techno", "Techno (Peak Time / Driving)", 0.95),
    ("Tech House; House", "Tech House", 0.95),          # varios: el primero reconocido
    ("(35)", "House", 0.95),                            # ID3v1 numérico
    ("Electronic", "Electronic", 0.3),                  # demasiado amplio
    ("Cumbia Rebajada", "Cumbia Rebajada", 0.6),        # no lo conocemos: se respeta
    ("", None, 0.0),
    (None, None, 0.0),
])
def test_genero_de_etiqueta(crudo, genero, conf):
    assert worker.genero_de_etiqueta(crudo) == (genero, conf)


def archivo(tmp_path, ext, genero):
    out = tmp_path / f"tema{ext}"
    meta = ["-metadata", f"genre={genero}"] if genero is not None else []
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    *meta, str(out)], check=True)
    return str(out)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="sin ffmpeg")
@pytest.mark.parametrize("ext", [".mp3", ".flac"])
def test_detectar_genero_desde_el_archivo(tmp_path, ext):
    g = worker.detectar_genero(archivo(tmp_path, ext, "Afro House"))
    assert g == {"genre_detected": "Afro House", "genre_confidence": 0.95, "genre_source": "etiqueta"}


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="sin ffmpeg")
def test_sin_etiqueta_queda_por_revisar(tmp_path):
    g = worker.detectar_genero(archivo(tmp_path, ".mp3", None))
    assert g["genre_detected"] is None and g["genero_por_revisar"] is True
