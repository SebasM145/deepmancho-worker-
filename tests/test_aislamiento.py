"""7.6.17: el análisis corre en un proceso hijo. Si muere (memoria, fallo en C), la réplica sigue y
el tema vuelve a la cola con un mensaje claro («Touched The Sky» mató 3 réplicas el 6-oct)."""
import os
import signal
import sys
import time

import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import worker  # noqa: E402

pytestmark = pytest.mark.aislamiento_real


@pytest.fixture(autouse=True)
def _aislado(monkeypatch):
    monkeypatch.setattr(worker, "AISLAR_ANALISIS", True)   # también en macOS para estas pruebas chicas


def _devuelve(x):
    return {"bpm": x, "pid": os.getpid()}


def _muere(_):
    os.kill(os.getpid(), signal.SIGKILL)


def _segfault(_):
    os.kill(os.getpid(), signal.SIGSEGV)


def _mudo(_):
    raise worker.AudioMudo("silencio")


def _ilegible(_):
    raise worker.ArchivoIlegible(worker.MSJ_ILEGIBLE)


def _rompe(_):
    raise ValueError("algo raro")


def _lento(_):
    time.sleep(30)


def test_devuelve_el_resultado_desde_otro_proceso():
    r = worker.en_proceso_aparte(_devuelve, 124.0)
    assert r["bpm"] == 124.0 and r["pid"] != os.getpid()


@pytest.mark.parametrize("fn,senal", [(_muere, "SIGKILL"), (_segfault, "SIGSEGV")])
def test_si_el_hijo_muere_el_padre_sigue_y_avisa(fn, senal, capsys):
    with pytest.raises(worker.AnalisisCaido) as e:
        worker.en_proceso_aparte(fn, None)
    assert str(e.value) == worker.MSJ_CAIDO
    assert senal in capsys.readouterr().out          # el log dice con qué señal murió


@pytest.mark.parametrize("fn,clase", [(_mudo, worker.AudioMudo), (_ilegible, worker.ArchivoIlegible)])
def test_las_excepciones_del_worker_viajan_con_su_clase(fn, clase):
    with pytest.raises(clase):
        worker.en_proceso_aparte(fn, None)


def test_otra_excepcion_viaja_como_error_con_su_nombre():
    with pytest.raises(RuntimeError, match="ValueError: algo raro"):
        worker.en_proceso_aparte(_rompe, None)


def test_el_tope_por_trabajo_corta_al_hijo():
    def _tope(_s, _f):
        raise TimeoutError("tope")
    viejo = signal.signal(signal.SIGALRM, _tope)
    signal.alarm(1)
    t0 = time.time()
    try:
        with pytest.raises(TimeoutError):
            worker.en_proceso_aparte(_lento, None)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, viejo)
    assert time.time() - t0 < 15


def test_un_trabajo_cuyo_analisis_muere_vuelve_a_la_cola_y_el_worker_sigue(monkeypatch, tmp_path):
    p = tmp_path / "a.mp3"
    p.write_bytes(b"x")
    enviados = []
    monkeypatch.setattr(worker, "download_audio", lambda url: str(p))
    monkeypatch.setattr(worker, "sondear_audio", lambda path: None)
    monkeypatch.setattr(worker, "analyze", lambda path, bpm_seed=None: os.kill(os.getpid(), signal.SIGKILL))
    monkeypatch.setattr(worker, "send_result", lambda *a, **k: enviados.append((a[2], k)))
    worker.process_job({"id": "j", "track_id": "t"}, {"artist": "a", "title": "b"}, "http://x/a.mp3")
    (estado, kw), = enviados
    assert estado == "error" and kw["error"] == worker.MSJ_CAIDO      # no determinista: se reintenta


@pytest.mark.skipif(sys.platform == "darwin", reason="fork tras hilos del sistema rompe al hijo en macOS; Railway es Linux")
def test_analisis_real_en_el_hijo_despues_de_calentar_el_padre(tmp_path, monkeypatch):
    """Lo mismo que en Railway: el padre ya usó librosa/numba (examen CM2 al arrancar) y después
    cada tema se analiza en un hijo hecho con fork. Tiene que dar lo mismo que en el padre."""
    import numpy as np
    import soundfile as sf
    monkeypatch.setattr(worker, "AISLAR_ANALISIS", True)
    sr = 22050
    t = np.arange(180 * sr) / sr
    f = t % (60 / 124)
    x = (np.sin(2 * np.pi * 55 * f) * np.exp(-f * 12) * 0.8 + 0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    p = tmp_path / "t.wav"
    sf.write(p, np.stack([x, x], 1), sr)
    en_padre = worker.analyze(str(p), bpm_seed=124.0)                 # calienta numba/OpenBLAS en el padre
    en_hijo = worker.en_proceso_aparte(worker.analyze, str(p), bpm_seed=124.0)
    assert en_hijo["bpm_precise"] == en_padre["bpm_precise"]
    assert en_hijo["cue_points"] == en_padre["cue_points"]
    sonoridad = worker.en_proceso_aparte(worker.medir_sonoridad, str(p))
    assert sonoridad.get("loudness_lufs") is not None
