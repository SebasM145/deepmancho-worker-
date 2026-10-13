"""Relevo de noche 13-oct: lo que quedaba sin prueba en el worker de pistas y en el verificador.

- Las órdenes a ffmpeg/ffprobe del worker de pistas (`ffprobe_duration`, `to_wav_mono`,
  `to_wav_estereo`, `codificar`, `_leer_estereo`, `_escribir_audio`): hasta hoy solo se
  probaban con ffmpeg de verdad. Aquí `subprocess.run` es un espía, así que corren en
  cualquier máquina y fijan la orden exacta y cómo se arma y se lee el audio crudo.
- La receta del motor híbrido (`separar_hibrido`, `_cargar_pesos`): sin torch, sin demucs
  y sin pesos. torch es un módulo falso sobre numpy y los tres modelos son dobles lineales,
  lo justo para fijar qué recibe cada modelo y que las seis pistas vuelven a sumar la mezcla.
- El modo banco del verificador (`grid_verifier.bench`) y la envolvente de graves sin scipy.

Fijan el comportamiento actual: no cambian código.
"""
import contextlib
import json
import os
import sys
import types
from pathlib import Path

import numpy as np
import numpy.testing  # noqa: F401  (se carga ya: al importarse llama a subprocess.run, que aquí es un espía)
import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import grid_verifier as gv  # noqa: E402
import stems_worker as sw  # noqa: E402


# ─────────────────────────────── órdenes a ffmpeg ───────────────────────────────
@pytest.fixture
def ffmpeg(monkeypatch):
    """Espía de `subprocess.run`: guarda cada llamada y responde con `salida`."""
    llamadas = []
    estado = types.SimpleNamespace(llamadas=llamadas, salida=b"")

    def run(cmd, **kw):
        llamadas.append((list(cmd), kw))
        return types.SimpleNamespace(stdout=estado.salida, stderr="", returncode=0)

    monkeypatch.setattr(sw.subprocess, "run", run)
    return estado


def valor_de(cmd, bandera):
    return cmd[cmd.index(bandera) + 1]


def intercalado(izq, der):
    """L R L R… en float32, como lo entrega ffmpeg con `-ac 2 -f f32le`."""
    return np.stack([izq, der], axis=1).astype(np.float32).tobytes()


def test_ffprobe_duration_lee_los_segundos(ffmpeg):
    ffmpeg.salida = "187.42\n"
    assert sw.ffprobe_duration(Path("/tmp/tema.mp3")) == pytest.approx(187.42)
    cmd, kw = ffmpeg.llamadas[0]
    assert cmd[0] == "ffprobe" and cmd[-1] == "/tmp/tema.mp3"
    assert valor_de(cmd, "-show_entries") == "format=duration"
    assert kw["check"] is True  # si ffprobe falla, se entera el que llama


def test_ffprobe_duration_sin_respuesta_es_cero(ffmpeg):
    ffmpeg.salida = "  \n"
    assert sw.ffprobe_duration(Path("vacio.mp3")) == 0.0


@pytest.mark.parametrize("sr", [22050, 44100])
def test_to_wav_mono_pide_mono_a_la_tasa_y_lee_float32(ffmpeg, sr):
    audio = np.linspace(-1, 1, 500, dtype=np.float32)
    ffmpeg.salida = audio.tobytes()
    y, tasa = sw.to_wav_mono(Path("a.mp3"), sr)
    assert tasa == sr and y.dtype == np.float32
    np.testing.assert_array_equal(y, audio)
    cmd, kw = ffmpeg.llamadas[0]
    assert cmd[0] == "ffmpeg" and valor_de(cmd, "-i") == "a.mp3"
    assert (valor_de(cmd, "-ac"), valor_de(cmd, "-ar"), valor_de(cmd, "-f")) == ("1", str(sr), "f32le")
    assert cmd[-1] == "-" and kw["check"] is True


def test_to_wav_mono_por_defecto_va_a_22050(ffmpeg):
    assert sw.to_wav_mono(Path("a.mp3"))[1] == 22050
    assert valor_de(ffmpeg.llamadas[0][0], "-ar") == "22050"


@pytest.mark.parametrize("leer,tasa", [(sw.to_wav_estereo, sw.SR_RENDER), (sw._leer_estereo, sw.SR_SEPARACION)])
def test_leer_estereo_separa_los_dos_canales(ffmpeg, leer, tasa):
    izq = np.arange(100, dtype=np.float32) / 100
    der = -izq
    ffmpeg.salida = intercalado(izq, der)
    y = leer(Path("a.mp3"))
    assert y.shape == (2, 100) and y.dtype == np.float32
    np.testing.assert_array_equal(y[0], izq)
    np.testing.assert_array_equal(y[1], der)
    y[0, 0] = 9.0  # es una copia propia: se puede escribir encima (frombuffer solo no deja)
    cmd, kw = ffmpeg.llamadas[0]
    assert (valor_de(cmd, "-ac"), valor_de(cmd, "-ar"), valor_de(cmd, "-f")) == ("2", str(tasa), "f32le")
    assert kw["check"] is True


@pytest.mark.parametrize("formato,codec", [("wav", ["-c:a", "pcm_s24le"]),
                                           ("mp3", ["-c:a", "libmp3lame", "-b:a", "320k"])])
def test_codificar_manda_el_audio_intercalado(ffmpeg, formato, codec):
    estereo = np.stack([np.linspace(0, 0.5, 64), np.linspace(0, -0.5, 64)])  # float64 a propósito
    dest = Path(f"/tmp/tema.{formato}")
    sw.codificar(estereo, dest, formato)
    cmd, kw = ffmpeg.llamadas[0]
    assert cmd[-1] == str(dest) and "-y" in cmd
    assert cmd[-1 - len(codec):-1] == codec
    # La entrada se declara ANTES de `-i -`: float32, estéreo, a la tasa del render.
    entrada = cmd[:cmd.index("-i")]
    assert (valor_de(entrada, "-f"), valor_de(entrada, "-ar"), valor_de(entrada, "-ac")) == ("f32le", str(sw.SR_RENDER), "2")
    enviado = np.frombuffer(kw["input"], dtype=np.float32).reshape(-1, 2).T
    np.testing.assert_allclose(enviado, estereo, atol=1e-7)
    assert kw["check"] is True


def test_codificar_lo_que_no_es_wav_sale_en_mp3(ffmpeg):
    sw.codificar(np.zeros((2, 8), dtype=np.float32), Path("tema.flac"), "flac")
    assert "libmp3lame" in ffmpeg.llamadas[0][0]


def test_escribir_audio_mp3_con_la_misma_cadena_que_demucs(ffmpeg):
    audio = np.stack([np.linspace(0, 0.9, 32), np.linspace(0, -0.9, 32)]).astype(np.float32)
    sw._escribir_audio(audio, Path("/tmp/drums.mp3"))
    cmd, kw = ffmpeg.llamadas[0]
    assert cmd[-1] == "/tmp/drums.mp3"
    assert valor_de(cmd, "-c:a") == "libmp3lame" and valor_de(cmd, "-b:a") == "192k"
    assert valor_de(cmd, "-write_xing") == "1"  # la cabecera con el retardo (1.22.3)
    assert (valor_de(cmd, "-ar"), valor_de(cmd, "-ac")) == (str(sw.SR_SEPARACION), "2")
    # Por debajo de 1 no se toca el volumen.
    np.testing.assert_array_equal(np.frombuffer(kw["input"], dtype=np.float32).reshape(-1, 2).T, audio)
    assert kw["check"] is True


def test_escribir_audio_wav_y_mono(ffmpeg):
    sw._escribir_audio(np.zeros((1, 16), dtype=np.float32), Path("voz.wav"), sr=48000)
    cmd, _ = ffmpeg.llamadas[0]
    assert valor_de(cmd, "-c:a") == "pcm_f32le" and "-write_xing" not in cmd
    assert (valor_de(cmd, "-ar"), valor_de(cmd, "-ac")) == ("48000", "1")


def test_escribir_audio_que_pasa_de_1_baja_la_pista_entera(ffmpeg):
    audio = np.array([[0.5, -2.0, 1.0], [0.25, 1.0, -0.5]], dtype=np.float32)
    antes = audio.copy()
    sw._escribir_audio(audio, Path("bass.mp3"))
    enviado = np.frombuffer(ffmpeg.llamadas[0][1]["input"], dtype=np.float32).reshape(-1, 2).T
    assert np.abs(enviado).max() == pytest.approx(1 / 1.01, rel=1e-5)  # justo por debajo de 1
    np.testing.assert_allclose(enviado, antes / (1.01 * 2.0), rtol=1e-6)  # misma forma, sin recortar picos
    np.testing.assert_array_equal(audio, antes)  # no se toca el audio del que llama


def test_escribir_audio_vacio_no_revienta(ffmpeg):
    sw._escribir_audio(np.zeros((2, 0), dtype=np.float32), Path("nada.mp3"))
    assert ffmpeg.llamadas[0][1]["input"] == b""


# ─────────────────────────────── híbrido: los pesos ───────────────────────────────
class ModeloFalso:
    def __init__(self):
        self.estado = None
        self.en_eval = False

    def load_state_dict(self, estado):
        self.estado = estado

    def eval(self):
        self.en_eval = True
        return self


@pytest.fixture
def torch_con_pesos(monkeypatch):
    """torch falso: `load` devuelve lo que la prueba deje en `archivo`."""
    falso = types.ModuleType("torch")
    falso.archivo = {}
    falso.pedidos = []

    def load(path, **kw):
        falso.pedidos.append((path, kw))
        return falso.archivo

    falso.load = load
    monkeypatch.setitem(sys.modules, "torch", falso)
    return falso


@pytest.mark.parametrize("archivo", [
    {"a.w": 1, "b.w": 2},
    {"state": {"a.w": 1, "b.w": 2}},
    {"state_dict": {"a.w": 1, "b.w": 2}, "epoch": 54},
    {"model": {"a.w": 1, "b.w": 2}},
    {"state": {"state_dict": {"a.w": 1, "b.w": 2}}},
    {"state_dict": {"module.a.w": 1, "module.b.w": 2}},  # guardado con DataParallel
])
def test_cargar_pesos_desenvuelve_el_archivo(torch_con_pesos, archivo):
    torch_con_pesos.archivo = archivo
    m = ModeloFalso()
    assert sw._cargar_pesos(m, Path("/pesos/x.ckpt")) is m
    assert m.estado == {"a.w": 1, "b.w": 2}
    assert m.en_eval is True
    path, kw = torch_con_pesos.pedidos[0]
    assert path == "/pesos/x.ckpt"
    # weights_only: un .ckpt bajado de internet no puede ejecutar código al cargarse.
    assert kw == {"map_location": "cpu", "weights_only": True}


def test_cargar_pesos_no_confunde_una_capa_llamada_model(torch_con_pesos):
    torch_con_pesos.archivo = {"model": 7, "module.model.w": 3}
    m = ModeloFalso()
    sw._cargar_pesos(m, Path("x.ckpt"))
    assert m.estado == {"model": 7, "model.w": 3}


# ─────────────────────────────── híbrido: la receta ───────────────────────────────
class Tensor(np.ndarray):
    """Lo mínimo de un tensor que usa `separar_hibrido`: aritmética de numpy y `.numpy()`."""

    def numpy(self):
        return np.asarray(self)


def tensor(a):
    return np.asarray(a).view(Tensor)


FUENTES_DEMUCS = ["drums", "bass", "other", "vocals", "guitar", "piano"]
PISTAS = ["vocals", "drums", "bass", "other", "piano", "guitar"]


@pytest.fixture
def hibrido(monkeypatch, tmp_path):
    """Todo lo pesado del híbrido, de mentira. Los modelos son lineales:
    voz = 25 % de la mezcla; batería 40 %, bajo 30 % y other 20 % del instrumental;
    piano 10 % y guitarra 5 % de lo que reciba demucs."""
    h = types.SimpleNamespace(hilos=[], modelos=[], pesos=[], trozos=[], demucs=[], escritos={})
    rng = np.random.default_rng(7)
    h.mezcla = (0.3 * rng.standard_normal((2, 4000))).astype(np.float32) + np.float32(0.05)

    torch = types.ModuleType("torch")
    torch.set_num_threads = h.hilos.append
    torch.from_numpy = tensor
    torch.inference_mode = contextlib.nullcontext
    monkeypatch.setitem(sys.modules, "torch", torch)

    class MelBandRoformer:
        def __init__(self, **kw):
            h.modelos.append(("mel", kw))

    class SCNet:
        def __init__(self, **kw):
            h.modelos.append(("scnet", kw))

    class Demucs:
        sources = FUENTES_DEMUCS
        en_eval = False

        def eval(self):
            self.en_eval = True

    def get_model(nombre):
        h.modelos.append(("demucs", nombre))
        h.dm = Demucs()
        return h.dm

    def apply_model(dm, x, **kw):
        h.demucs.append((np.array(x), kw, dm.en_eval))
        partes = {"piano": 0.10, "guitar": 0.05}
        return tensor(np.stack([np.float32(partes.get(f, 0.0)) * x[0] for f in dm.sources])[None])

    def modulo(nombre, **cosas):
        m = types.ModuleType(nombre)
        for k, v in cosas.items():
            setattr(m, k, v)
        monkeypatch.setitem(sys.modules, nombre, m)

    modulo("models")
    modulo("models.bs_roformer")
    modulo("models.bs_roformer.mel_band_roformer", MelBandRoformer=MelBandRoformer)
    modulo("models.scnet")
    modulo("models.scnet.scnet", SCNet=SCNet)
    modulo("demucs")
    modulo("demucs.apply", apply_model=apply_model)
    modulo("demucs.pretrained", get_model=get_model)

    def separar_por_trozos(modelo, mezcla, trozo, solapes, lote=1):
        h.trozos.append((type(modelo).__name__, np.array(mezcla), trozo, solapes, lote))
        if isinstance(modelo, MelBandRoformer):
            return tensor(0.25 * mezcla)[None]
        partes = {"drums": 0.4, "bass": 0.3, "other": 0.2, "vocals": 0.0}
        return tensor(np.stack([np.float32(partes[f]) * mezcla for f in sw.SCNET_XL["sources"]]))

    def cargar(modelo, path):
        h.pesos.append((type(modelo).__name__, path))
        return modelo

    h.base = tmp_path / "pesos"
    monkeypatch.setattr(sw, "asegurar_hibrido", lambda: h.base)
    monkeypatch.setattr(sw, "_cargar_pesos", cargar)
    monkeypatch.setattr(sw, "separar_por_trozos", separar_por_trozos)
    monkeypatch.setattr(sw, "_leer_estereo", lambda src: h.mezcla.copy())
    monkeypatch.setattr(sw, "_escribir_audio", lambda audio, dest: h.escritos.__setitem__(dest, np.array(audio)))
    monkeypatch.setattr(sw, "log", lambda *a: None)
    monkeypatch.setattr(sys, "path", list(sys.path))  # separar_hibrido agrega el código de los modelos
    for var in ("HIBRIDO_LOTE", "HIBRIDO_SOLAPES", "HIBRIDO_OTHER"):
        monkeypatch.delenv(var, raising=False)
    h.src, h.out = tmp_path / "tema.mp3", tmp_path / "salida" / "hibrido"
    return h


def pista(h, nombre, ext=".mp3"):
    return h.escritos[h.out / f"{nombre}{ext}"]


def test_hibrido_entrega_las_seis_pistas_de_siempre(hibrido):
    salida = sw.separar_hibrido(hibrido.src, hibrido.out)
    # Mismos nombres que htdemucs_6s: la plataforma no distingue un motor del otro.
    assert list(salida) == PISTAS
    assert salida == {p: hibrido.out / f"{p}.mp3" for p in PISTAS}
    assert set(hibrido.escritos) == set(salida.values())
    assert hibrido.out.is_dir()
    assert all(a.shape == hibrido.mezcla.shape for a in hibrido.escritos.values())


def test_hibrido_las_seis_pistas_vuelven_a_sumar_la_mezcla(hibrido):
    sw.separar_hibrido(hibrido.src, hibrido.out)
    suma = sum(pista(hibrido, p) for p in PISTAS)
    np.testing.assert_allclose(suma, hibrido.mezcla, atol=1e-5)
    # Y «other» no es el de SCNet (20 %): es lo que sobra después de las otras cinco.
    inst = 0.75 * hibrido.mezcla
    assert not np.allclose(pista(hibrido, "other"), 0.2 * inst, atol=1e-3)


def test_hibrido_cada_modelo_recibe_lo_que_le_toca(hibrido):
    sw.separar_hibrido(hibrido.src, hibrido.out)
    mezcla = hibrido.mezcla
    (m1, x1, *_), (m2, x2, *_) = hibrido.trozos
    # 1) La voz sale de la mezcla completa.
    assert m1 == "MelBandRoformer"
    np.testing.assert_array_equal(x1, mezcla)
    np.testing.assert_allclose(pista(hibrido, "vocals"), 0.25 * mezcla, atol=1e-6)
    # 2) Batería y bajo salen del instrumental (mezcla − voz), no de la mezcla.
    assert m2 == "SCNet"
    inst = mezcla - 0.25 * mezcla
    np.testing.assert_allclose(x2, inst, atol=1e-6)
    np.testing.assert_allclose(pista(hibrido, "drums"), 0.4 * inst, atol=1e-6)
    np.testing.assert_allclose(pista(hibrido, "bass"), 0.3 * inst, atol=1e-6)


def test_hibrido_demucs_recibe_el_instrumental_normalizado_y_se_deshace(hibrido):
    sw.separar_hibrido(hibrido.src, hibrido.out)
    inst = 0.75 * hibrido.mezcla
    ref = inst.mean(0)
    media, desvio = ref.mean(), ref.std() + 1e-8
    (x, kw, en_eval), = hibrido.demucs
    assert en_eval is True
    assert x.shape == (1,) + inst.shape  # un lote de un tema
    np.testing.assert_allclose(x[0], (inst - media) / desvio, atol=1e-5)
    # Los mismos parámetros que el demucs de siempre (el CLI).
    assert kw == {"shifts": 1, "split": True, "overlap": float(sw.OVERLAP), "segment": float(sw.SEGMENT),
                  "num_workers": 0, "progress": False}
    # La salida vuelve a la escala del tema: 10 % y 5 % de (inst − media), más la media.
    np.testing.assert_allclose(pista(hibrido, "piano"), 0.10 * (inst - media) + media, atol=1e-5)
    np.testing.assert_allclose(pista(hibrido, "guitar"), 0.05 * (inst - media) + media, atol=1e-5)
    assert abs(media) > 1e-3  # la mezcla de prueba no está centrada: la media sí cuenta


def test_hibrido_arma_los_modelos_con_su_configuracion_y_sus_pesos(hibrido):
    sw.separar_hibrido(hibrido.src, hibrido.out)
    assert hibrido.modelos == [("mel", sw.MEL_KIM), ("scnet", sw.SCNET_XL), ("demucs", "htdemucs_6s")]
    assert hibrido.pesos == [("MelBandRoformer", hibrido.base / "MelBandRoformer.ckpt"),
                             ("SCNet", hibrido.base / "model_scnet_ep_54_sdr_9.8051.ckpt")]
    # Los pesos que se cargan son los mismos que `asegurar_hibrido` baja y verifica.
    assert {p.name for _, p in hibrido.pesos} == set(sw.HIBRIDO_PESOS)
    assert str(hibrido.base / "msst") in sys.path


def test_hibrido_trozos_y_solapes_de_cada_modelo(hibrido):
    sw.separar_hibrido(hibrido.src, hibrido.out)
    assert [t[2:] for t in hibrido.trozos] == [(sw.MEL_KIM_TROZO, sw.MEL_KIM_SOLAPES, 1),
                                               (sw.SCNET_XL_TROZO, sw.SCNET_XL_SOLAPES, 1)]


def test_hibrido_solapes_y_lote_por_variable(hibrido, monkeypatch):
    monkeypatch.setenv("HIBRIDO_SOLAPES", "4")
    monkeypatch.setenv("HIBRIDO_LOTE", "3")
    sw.separar_hibrido(hibrido.src, hibrido.out)
    assert [t[2:] for t in hibrido.trozos] == [(sw.MEL_KIM_TROZO, 4, 3), (sw.SCNET_XL_TROZO, 4, 3)]


@pytest.mark.parametrize("lote", ["0", "-2"])
def test_hibrido_el_lote_nunca_baja_de_uno(hibrido, monkeypatch, lote):
    monkeypatch.setenv("HIBRIDO_LOTE", lote)
    sw.separar_hibrido(hibrido.src, hibrido.out)
    assert [t[4] for t in hibrido.trozos] == [1, 1]


@pytest.mark.parametrize("valor", ["scnet", " SCNet "])
def test_hibrido_other_de_scnet_ya_no_suma_la_mezcla(hibrido, monkeypatch, valor):
    monkeypatch.setenv("HIBRIDO_OTHER", valor)
    sw.separar_hibrido(hibrido.src, hibrido.out)
    inst = 0.75 * hibrido.mezcla
    esperado = 0.2 * inst - pista(hibrido, "piano") - pista(hibrido, "guitar")
    np.testing.assert_allclose(pista(hibrido, "other"), esperado, atol=1e-5)
    assert not np.allclose(sum(pista(hibrido, p) for p in PISTAS), hibrido.mezcla, atol=1e-3)


def test_hibrido_un_valor_raro_de_other_es_el_resto(hibrido, monkeypatch):
    monkeypatch.setenv("HIBRIDO_OTHER", "cualquiera")
    sw.separar_hibrido(hibrido.src, hibrido.out)
    np.testing.assert_allclose(sum(pista(hibrido, p) for p in PISTAS), hibrido.mezcla, atol=1e-5)


@pytest.mark.parametrize("hilos,esperado", [("3", 3), ("", 6), (None, 6)])
def test_hibrido_hilos_de_torch(hibrido, monkeypatch, hilos, esperado):
    if hilos is None:
        monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
    else:
        monkeypatch.setenv("OMP_NUM_THREADS", hilos)
    sw.separar_hibrido(hibrido.src, hibrido.out)
    assert hibrido.hilos == [esperado]


def test_hibrido_con_otra_extension(hibrido):
    salida = sw.separar_hibrido(hibrido.src, hibrido.out, ext=".wav")
    assert [p.name for p in salida.values()] == [f"{p}.wav" for p in PISTAS]


def test_hibrido_si_un_modelo_falla_no_deja_pistas_a_medias(hibrido, monkeypatch):
    def sin_memoria(modelo, mezcla, trozo, solapes, lote=1):
        raise MemoryError("sin memoria")

    monkeypatch.setattr(sw, "separar_por_trozos", sin_memoria)
    with pytest.raises(MemoryError):
        sw.separar_hibrido(hibrido.src, hibrido.out)
    assert hibrido.escritos == {}  # se escribe todo al final: `separar` puede seguir con demucs


def test_separar_cae_en_demucs_si_el_hibrido_revienta_adentro(hibrido, monkeypatch, tmp_path):
    """El respaldo de `separar` con el híbrido de verdad fallando a mitad (no un doble entero)."""
    def sin_memoria(modelo, mezcla, trozo, solapes, lote=1):
        raise MemoryError("sin memoria")

    monkeypatch.setattr(sw, "separar_por_trozos", sin_memoria)
    monkeypatch.setattr(sw.traceback, "print_exc", lambda: None)
    pedidos = []
    monkeypatch.setattr(sw, "run_demucs", lambda src, out, model: pedidos.append((src, out, model)) or {"drums": out / "d.mp3"})
    stems, info = sw.separar(hibrido.src, tmp_path / "salida", {"model": "hibrido"})
    assert stems == {"drums": tmp_path / "salida" / "d.mp3"}
    assert pedidos == [(hibrido.src, tmp_path / "salida", sw.modelo_demucs(None))]
    assert info["engine"] == "demucs" and "sin memoria" in info["engine_fallback"]


# ─────────────────────────────── verificador: modo banco ───────────────────────────────
@pytest.fixture
def banco(monkeypatch):
    """`decode_file` y `measure_track` de mentira: guarda el tema que arma `bench`."""
    visto = {}
    pcm = np.zeros(10, dtype=np.float32)

    def measure_track(track, audio, sr):
        visto.update(track=track, audio=audio, sr=sr)
        return {"pick": {"bpm": 124.25, "deltaAnchorMs": 1.5}, "conf": 0.91, "passesGate": True}

    monkeypatch.setattr(gv, "decode_file", lambda path: visto.update(path=path) or (pcm, 44100))
    monkeypatch.setattr(gv, "measure_track", measure_track)
    visto["pcm"] = pcm
    return visto


def test_bench_arma_el_tema_con_bpm_y_ancla(banco, capsys):
    gv.bench(["tema.mp3", "124", "35.5"])
    assert banco["path"] == "tema.mp3" and banco["sr"] == 44100 and banco["audio"] is banco["pcm"]
    t = banco["track"]
    assert t == {"bpm": 124, "bpm_fine": 0.0, "bpm_tag": None, "bpm_precise": None, "bpm_detected": None,
                 "first_beat_offset_ms": 35.5, "first_beat_detected_ms": 35.5, "grid_source": "detectada"}
    assert gv.eff_bpm(t) == 124 and gv.first_beat_ms_of(t) == 35.5
    salida = json.loads(capsys.readouterr().out)
    assert salida["pick"] == {"bpm": 124.25, "deltaAnchorMs": 1.5}
    assert salida["conf"] == 0.91 and salida["passesGate"] is True
    assert isinstance(salida["decodeMs"], int) and salida["decodeMs"] >= 0


def test_bench_con_ajuste_fino_y_etiqueta(banco, capsys):
    gv.bench(["tema.mp3", "124", "35", "0.25", "124.3"])
    t = banco["track"]
    assert (t["bpm_fine"], t["bpm_tag"]) == (0.25, 124.3)
    assert gv.eff_bpm(t) == pytest.approx(124.25)  # tempo = bpm + bpm_fine
    assert len(capsys.readouterr().out.strip().splitlines()) == 1  # una sola línea JSON


def test_bench_si_la_medida_falla_no_imprime_nada(banco, monkeypatch, capsys):
    def falla(track, audio, sr):
        raise RuntimeError("too_short")

    monkeypatch.setattr(gv, "measure_track", falla)
    with pytest.raises(RuntimeError, match="too_short"):
        gv.bench(["tema.mp3", "124", "0"])
    assert capsys.readouterr().out == ""


# ─────────────────────────────── verificador: envolvente sin scipy ───────────────────────────────
def test_low_envelope_sin_scipy_da_lo_mismo(monkeypatch):
    rng = np.random.default_rng(3)
    pcm = (0.4 * rng.standard_normal(4410)).astype(np.float32)
    con_scipy, tasa = gv.low_envelope(pcm, 44100)
    monkeypatch.setitem(sys.modules, "scipy.signal", None)  # `from scipy.signal import …` falla
    sin_scipy, tasa2 = gv.low_envelope(pcm, 44100)
    assert tasa == tasa2 and sin_scipy.dtype == np.float32
    assert len(sin_scipy) == len(con_scipy) > 0
    np.testing.assert_allclose(sin_scipy, con_scipy, rtol=1e-5, atol=1e-7)
