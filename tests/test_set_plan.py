"""#143: «Convertir en set» manda el plan de cada transición en spec.transiciones y el
render tiene que seguirlo (mismo método que suena en las listas del navegador)."""
import os
import shutil
import sys

import numpy as np
import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import worker  # noqa: E402

SR = worker.SET_SR
hay_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="sin ffmpeg")


def golpes(bpm, dur_s, hz, silencio_final_s=0.0):
    """Clic de `hz` en cada negra: sirve para medir dónde y a qué tempo suena cada tema."""
    t = np.arange(int(dur_s * SR)) / SR
    fase = t % (60.0 / bpm)
    largo = 0.04  # ráfaga con ventana de Hann: no se filtra a otras frecuencias
    ventana = np.where(fase < largo, 0.5 - 0.5 * np.cos(2 * np.pi * fase / largo), 0.0)
    x = (0.5 * ventana * np.sin(2 * np.pi * hz * t)).astype(np.float32)
    x = np.stack([x, x], axis=1)
    return np.vstack([x, np.zeros((int(silencio_final_s * SR), 2), np.float32)])


def energia_en(x, hz, ini_s, fin_s):
    """Energía alrededor de `hz` entre dos segundos (FFT del tramo)."""
    seg = x[int(ini_s * SR):int(fin_s * SR), 0]
    if len(seg) == 0:
        return 0.0
    esp = np.abs(np.fft.rfft(seg))
    f = np.fft.rfftfreq(len(seg), 1 / SR)
    return float(np.sum(esp[(f > hz * 0.9) & (f < hz * 1.1)] ** 2) / len(seg))


def transicion(tipo, **k):
    base = {"desde": "a", "hasta": "b", "tipo": tipo, "salida_seg": 20.0, "entrada_seg": 0.0,
            "duracion_seg": 0.0, "compases": 0, "rate": None, "release_seg": None, "tempo_ratio": None,
            "asimetria": None, "graves_swap": False, "graves_swap_en": None, "razon": None}
    return {**base, **k}


def render(monkeypatch, audios, transiciones):
    """render_set con descargas y subida simuladas; devuelve (salida, tracklist)."""
    tracks = [{"id": tid, "title": tid, "bpm": bpm, "audio_url": f"http://audio/{tid}"}
              for tid, bpm in audios]
    pcm = {f"http://audio/{tid}": a for (tid, _), a in zip(audios, AUDIOS[0])}

    class R:
        status_code = 200

        def __init__(self, url):
            self.content = url.encode()

        def raise_for_status(self):
            pass

    monkeypatch.setattr(worker.requests, "get", lambda url, timeout=None: R(url))
    monkeypatch.setattr(worker, "_decode_pcm", lambda p: pcm[open(p, "rb").read().decode()].copy())
    capt = {}

    def subir(salida, tmpdir, upload_url, result_path):
        capt["salida"] = salida
        return len(salida) / SR

    monkeypatch.setattr(worker, "_masterizar_y_subir", subir)
    _, tracklist = worker.render_set({"spec": {"transiciones": transiciones}}, tracks, "u", "r")
    return capt["salida"], tracklist


AUDIOS = [None]


def con_audios(*arrays):
    AUDIOS[0] = arrays


def test_plan_valido():
    tracks = [{"id": "a"}, {"id": "b"}, {"track_id": "c"}]
    ok = [transicion("mezcla"), {**transicion("eco"), "desde": "b", "hasta": "c"}]
    assert worker.plan_valido(tracks, ok)
    assert not worker.plan_valido(tracks, ok[:1])                             # falta un par
    assert not worker.plan_valido(tracks, [ok[1], ok[0]])                     # otro orden
    assert not worker.plan_valido(tracks, [ok[0], {**ok[1], "tipo": "x"}])     # tipo desconocido
    assert not worker.plan_valido(tracks, None)


def test_ganancias_iguales_al_navegador():
    t = np.linspace(0, 1, 101)
    g_out, g_in = worker.ganancias_mezcla(t, 0.6)
    assert np.allclose(g_out ** 2 + g_in ** 2, 1.0)            # potencia constante
    a, b = worker.ganancias_mezcla(np.array([0.6]), 0.6)
    assert abs(a[0] - b[0]) < 1e-9                              # a la par en la asimetría
    assert g_out[0] == pytest.approx(1) and g_in[-1] == pytest.approx(1)


def test_graves_con_swap():
    t = np.linspace(0, 1, 101)
    db_in, db_out = worker.curvas_graves(t, True, 0.5)
    assert db_in[0] == -12 and db_out[0] == 0                  # antes del swap: graves de la saliente
    assert db_in[-1] == 0 and db_out[-1] == -12                 # después: los de la entrante
    db_in, db_out = worker.curvas_graves(t, False, 0.5)
    assert db_in[0] == -6 and db_in[-1] == 0 and not db_out.any()


def test_seg_en_linea():
    tramos = [(10.0, 0.0, 0.98), (10.0 + 8 * 0.98, 8.0, 0.99), (10.0 + 8 * 0.98 + 4 * 0.99, 12.0, 1.0)]
    assert worker.seg_en_linea(tramos, 10.0) == pytest.approx(0.0)
    assert worker.seg_en_linea(tramos, 10.0 + 8 * 0.98) == pytest.approx(8.0)
    assert worker.seg_en_linea(tramos, 10.0 + 8 * 0.98 + 4 * 0.99 + 5) == pytest.approx(17.0)


def test_corte_entra_justo_en_el_compas(monkeypatch):
    con_audios(golpes(124, 40, 440), golpes(124, 30, 1500))
    sal, tl = render(monkeypatch, [("a", 124), ("b", 124)], [transicion("corte", salida_seg=20.0)])
    assert tl[1]["start_seconds"] == pytest.approx(20.0, abs=1e-3)  # con milisegundos (P2 de #75)
    assert len(sal) == pytest.approx((20 + 30) * SR, abs=2)
    b = energia_en(sal, 1500, 20.1, 25)
    assert energia_en(sal, 1500, 0, 19.9) < b * 1e-4
    assert energia_en(sal, 440, 20.1, 50) < energia_en(sal, 440, 0, 19.9) * 1e-4


def test_encadenado_al_final_util(monkeypatch):
    con_audios(golpes(124, 30, 440, silencio_final_s=5), golpes(124, 10, 1500))
    sal, tl = render(monkeypatch, [("a", 124), ("b", 124)], [transicion("encadenado", salida_seg=None)])
    assert 29.4 < tl[1]["start_seconds"] < 30.0  # tras el último golpe, sin los 5 s de silencio                   # no espera los 5 s de silencio
    assert len(sal) < 41 * SR


def test_eco_deja_cola_a_tempo(monkeypatch):
    con_audios(golpes(120, 40, 440), golpes(120, 30, 1500))
    sal, tl = render(monkeypatch, [("a", 120), ("b", 120)], [transicion("eco", salida_seg=20.0)])
    assert tl[1]["start_seconds"] == pytest.approx(20.0, abs=1e-3)
    cola = energia_en(sal, 440, 20.5, 22)                       # repeticiones del eco
    assert cola > 0
    assert energia_en(sal, 440, 26, 50) < cola * 0.05            # se apaga en ~8 beats (4 s)


@hay_ffmpeg
def test_mezcla_iguala_tempo_y_respeta_el_plan(monkeypatch):
    rate = 124 / 126  # la entrante (126) baja a 124 durante la mezcla
    con_audios(golpes(124, 60, 440), golpes(126, 60, 1500))
    plan = transicion("mezcla", salida_seg=30.0, entrada_seg=2.0, duracion_seg=16 * 240 / 124,
                      compases=16, rate=rate, release_seg=8 * 240 / 126, asimetria=0.6,
                      graves_swap=True, graves_swap_en=0.5)
    sal, tl = render(monkeypatch, [("a", 124), ("b", 126)], [plan])
    dur = plan["duracion_seg"]
    assert tl[1]["start_seconds"] == pytest.approx(30.0, abs=1e-3)
    a, b = energia_en(sal, 440, 0, 29.9), energia_en(sal, 1500, 30 + dur + 0.1, 30 + dur + 5)
    assert energia_en(sal, 1500, 0, 29.9) < b * 1e-4              # antes del plan no suena la entrante
    assert energia_en(sal, 440, 30 + dur + 0.1, 30 + dur + 5) < a * 1e-4   # después solo la entrante
    # Durante la mezcla los golpes de la entrante caen a 124 BPM, como la saliente.
    seg = sal[int((30 + 2) * SR):int((30 + dur - 2) * SR), 0]
    f = np.fft.rfftfreq(len(seg), 1 / SR)
    banda = np.fft.irfft(np.fft.rfft(seg) * ((f > 1350) & (f < 1650)), len(seg))
    suave = np.convolve(np.abs(banda), np.ones(int(0.01 * SR)) / int(0.01 * SR), mode="same")  # envolvente
    env = suave > 0.3 * np.max(suave)
    ini = np.nonzero(env[1:] & ~env[:-1])[0] / SR
    gaps = np.diff(ini)
    intervalo = np.median(gaps[gaps > 0.3])
    assert intervalo == pytest.approx(60 / 124, abs=0.004)
    # Largo total: 30 s + lo que queda de la entrante (estirada en mezcla y rampa).
    usado = plan["entrada_seg"] + dur * rate + plan["release_seg"] * (1 + rate) / 2
    esperado = 30 + dur + plan["release_seg"] + (60 - usado)
    assert len(sal) / SR == pytest.approx(esperado, abs=0.2)


def test_sin_plan_usa_el_metodo_anterior(monkeypatch):
    llamado = {}
    monkeypatch.setattr(worker, "_mezcla_libre", lambda tracks, d: llamado.setdefault("si", (np.zeros((10, 2), np.float32), [])))
    monkeypatch.setattr(worker, "_masterizar_y_subir", lambda s, d, u, r: 0.0)
    worker.render_set({"spec": {}}, [{"id": "a"}, {"id": "b"}], "u", "r")
    assert "si" in llamado


def test_sin_estirar_pasa_con_eco(monkeypatch, capsys):
    """Revisión del integrador: si no se puede igualar el tempo, nunca cruzar dos tempos."""
    con_audios(golpes(124, 40, 440), golpes(126, 30, 1500))
    monkeypatch.setattr(worker, "_estirar_pcm", lambda x, tempo, d: None)
    plan = transicion("mezcla", salida_seg=20.0, duracion_seg=8.0, rate=124 / 126, release_seg=4.0)
    sal, tl = render(monkeypatch, [("a", 124), ("b", 126)], [plan])
    assert "pasa con eco" in capsys.readouterr().out
    assert tl[1]["start_seconds"] == pytest.approx(20.0, abs=1e-3)
    assert len(sal) == pytest.approx((20 + 30) * SR, abs=2)      # la entrante entera, sin estirar


def test_descarga_fallida_sin_url_firmada(monkeypatch, tmp_path):
    class R:
        status_code = 403

    monkeypatch.setattr(worker.requests, "get", lambda url, timeout=None: R())
    with pytest.raises(RuntimeError) as e:
        worker._bajar_tema("https://x.supabase.co/storage/v1/object/sign/music/a.mp3?token=eyJsecreto", str(tmp_path / "a"))
    assert "eyJ" not in str(e.value) and "403" in str(e.value) and "x.supabase.co" in str(e.value)


def test_sin_firmas():
    msg = "404 for url: https://x.supabase.co/storage/v1/object/sign/a.mp3?token=eyJabc&x=1 al bajar"
    limpio = worker._sin_firmas(msg)
    assert "eyJ" not in limpio and "https://x.supabase.co/storage/v1/object/sign/a.mp3?…" in limpio
