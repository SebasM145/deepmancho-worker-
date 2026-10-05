"""#75 C-2: el render de un set no puede crecer en memoria con el largo del set.

Antes el set entero vivía en memoria y cada transición lo copiaba: 28 temas (163 min)
llegaban a 13,97 GB en una réplica de 10 GB. Ahora se escribe a disco por tramos. Esta
prueba mide con tracemalloc (numpy le reporta sus arreglos) el pico de un set de 4 temas
y de uno de 16, por el camino real del plan y del máster (solo la red y el MP3 simulados):
el pico tiene que ser casi el mismo y del orden de un tema. Con el render anterior falla."""
import os
import shutil
import sys
import tracemalloc

import numpy as np
import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import worker  # noqa: E402

SR = worker.SET_SR
DUR_TEMA = 30  # s
BYTES_TEMA = DUR_TEMA * SR * 2 * 4  # estéreo float32: ~10,6 MB


def tema(i):
    t = np.arange(DUR_TEMA * SR, dtype=np.float32) / SR
    fase = np.mod(t, np.float32(0.5))  # 120 BPM
    x = (0.6 * np.exp(-fase * 30, dtype=np.float32) * np.sin(2 * np.pi * 55 * fase, dtype=np.float32)
         + 0.05 * np.sin(2 * np.pi * (220 + 10 * i) * t, dtype=np.float32)).astype(np.float32)
    return np.stack([x, x], axis=1)


@pytest.fixture
def sin_red(monkeypatch):
    monkeypatch.setattr(worker, "_bajar_tema", lambda url, dest: open(dest, "w").write(url.rsplit("/", 1)[1]))
    monkeypatch.setattr(worker, "_decode_pcm", lambda p, sr=SR: tema(int(open(p).read())))

    def run(cmd, check=False, **k):  # el «MP3» es una copia del WAV: sin ffmpeg
        shutil.copy(cmd[cmd.index("-i") + 1], cmd[-1])

    class Subida:
        status_code = 200

        def raise_for_status(self):
            pass

    monkeypatch.setattr(worker.subprocess, "run", run)
    monkeypatch.setattr(worker.requests, "put", lambda *a, **k: Subida())


def pico_de_un_set(n):
    tracks = [{"id": str(i), "title": str(i), "bpm": 120, "audio_url": f"http://audio/{i}"} for i in range(n)]
    trans = [{"desde": str(i), "hasta": str(i + 1), "tipo": "mezcla" if i % 3 else "eco",
              "salida_seg": DUR_TEMA - 10.0, "entrada_seg": 0.0, "duracion_seg": 8.0, "compases": 4,
              "rate": None, "release_seg": None, "asimetria": 0.6, "graves_swap": True, "graves_swap_en": 0.5,
              "tempo_ratio": 1, "razon": None} for i in range(n - 1)]
    tracemalloc.start()
    try:
        dur, tl = worker.render_set({"spec": {"transiciones": trans}}, tracks, "http://subir/x", "x.mp3")
        pico = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert len(tl) == n and dur > (n - 1) * (DUR_TEMA - 10)
    return pico


def test_el_pico_no_crece_con_el_largo_del_set(sin_red):
    corto, largo = pico_de_un_set(4), pico_de_un_set(16)
    print(f"pico 4 temas: {corto / 1e6:.0f} MB · 16 temas: {largo / 1e6:.0f} MB · un tema: {BYTES_TEMA / 1e6:.0f} MB")
    assert largo < 1.5 * corto, (corto, largo)          # antes: ~4× (crece con el set)
    assert largo < 8 * BYTES_TEMA, largo                # del orden de un tema, no del set


def test_set_en_disco_se_puede_leer_por_trozos(tmp_path):
    d = worker.SetEnDisco(str(tmp_path / "s.f32"))
    a, b = tema(0), tema(1)
    d.escribir(a)
    d.escribir(b)
    d.cerrar()
    assert len(d) == len(a) + len(b)
    junto = np.concatenate(list(d.trozos(frames=SR * 7)))
    assert np.array_equal(junto, np.vstack([a, b]))
    assert np.array_equal(d[len(a):len(a) + 5], b[:5])


def test_lufs_por_trozos_igual_a_pyloudnorm():
    import pyloudnorm as pyln
    x = np.vstack([tema(0) * 0.3, tema(1)])               # dos niveles: entran las puertas
    esperado = pyln.Meter(SR).integrated_loudness(x.mean(axis=1))
    lufs, pico = worker._lufs_y_pico(x)
    assert lufs == pytest.approx(esperado, abs=0.05)
    assert pico == pytest.approx(float(np.max(np.abs(x))))
