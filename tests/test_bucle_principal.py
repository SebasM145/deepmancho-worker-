"""Bucle principal del worker de análisis (worker.main): qué cola se atiende primero, qué pasa
cuando la plataforma no responde y qué hace con el examen CM2 al arrancar. Sin cambiar código.

La espera creciente (W2) está en tests/test_espera.py y el apagado en tests/test_apagado.py;
aquí va el resto del bucle: el orden análisis → set → parecido → limpieza de etiquetas."""
import os
import signal
import sys

import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import worker  # noqa: E402


class Basta(BaseException):
    """Corta el bucle infinito de main() en la prueba. No es Exception a propósito: main() atrapa
    cualquier Exception de next_job (y sigue), así que una normal no lo sacaría del bucle."""


class Bucle:
    """main() con todas sus colas simuladas. `pasos` dice qué hay en cada vuelta: "analisis",
    "set", "parecido", "limpieza", "error" (la plataforma no responde) o None (todo vacío).
    Al acabarse los pasos, la siguiente consulta corta el bucle."""

    def __init__(self, monkeypatch, pasos, set_render=True, subidas=None):
        self.pasos = iter(pasos)
        self.paso = None
        self.subidas = subidas
        self.hecho = []       # todo lo que el bucle hizo, en orden
        self.esperas = []     # cada time.sleep
        self.procesados = []  # argumentos de process_job
        monkeypatch.setattr(worker, "filtrar_salida", lambda: None)
        monkeypatch.setattr(worker, "GOLDEN_EXAM", False)
        monkeypatch.setattr(worker, "APAGANDO", False)
        monkeypatch.setattr(worker, "EXAMEN_CM2_APROBADO", False)
        monkeypatch.setattr(worker, "ENABLE_SET_RENDER", set_render)
        monkeypatch.setattr(worker, "next_job", self.next_job)
        monkeypatch.setattr(worker, "process_job", self.process_job)
        monkeypatch.setattr(worker, "poll_set_render", lambda: self.cola("set"))
        monkeypatch.setattr(worker, "poll_parecido_calibracion", lambda: self.cola("parecido"))
        monkeypatch.setattr(worker, "poll_limpiar_streams", lambda: self.cola("limpieza"))
        monkeypatch.setattr(worker, "liberar_memoria", lambda: self.hecho.append("memoria"))
        monkeypatch.setattr(worker.time, "sleep", self.sleep)

    def next_job(self):
        try:
            self.paso = next(self.pasos)
        except StopIteration:
            raise Basta from None
        self.hecho.append("pide")
        if self.paso == "error":
            raise RuntimeError("la plataforma no responde")
        if self.paso == "analisis":
            return {"id": "j", "track_id": "t"}, {"bpm": 124}, "http://audio/x", self.subidas
        return None, None, None, None

    def process_job(self, *args):
        self.hecho.append("analiza")
        self.procesados.append(args)

    def cola(self, nombre):
        self.hecho.append(f"mira {nombre}")
        return self.paso == nombre

    def sleep(self, s):
        self.hecho.append("espera")
        self.esperas.append(s)

    def correr(self):
        with pytest.raises(Basta):
            worker.main()
        return self


@pytest.fixture(autouse=True)
def senal_restaurada():
    """main() instala su manejador de SIGTERM: se devuelve el que había."""
    viejo = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, viejo)


# ----------------------------------------------------------------------------- prioridad de las colas
def test_con_un_tema_por_analizar_no_mira_ninguna_otra_cola(monkeypatch):
    b = Bucle(monkeypatch, ["analisis", "analisis"]).correr()
    assert b.hecho == ["pide", "analiza", "memoria", "pide", "analiza", "memoria"]
    assert b.esperas == []                       # con trabajo no se duerme entre temas


def test_con_la_cola_vacia_mira_set_parecido_y_limpieza_en_ese_orden(monkeypatch):
    b = Bucle(monkeypatch, [None]).correr()
    assert b.hecho == ["pide", "mira set", "mira parecido", "mira limpieza", "espera"]


def test_un_set_por_renderizar_va_antes_que_el_parecido_y_la_limpieza(monkeypatch):
    b = Bucle(monkeypatch, ["set"]).correr()
    assert b.hecho == ["pide", "mira set", "memoria"]     # vuelve a pedir temas sin dormir


def test_sin_set_render_habilitado_ni_se_consulta_esa_cola(monkeypatch):
    b = Bucle(monkeypatch, ["set", None], set_render=False).correr()
    assert "mira set" not in b.hecho
    assert b.hecho[:4] == ["pide", "mira parecido", "mira limpieza", "espera"]


def test_el_parecido_va_antes_que_la_limpieza(monkeypatch):
    b = Bucle(monkeypatch, ["parecido"]).correr()
    assert b.hecho == ["pide", "mira set", "mira parecido", "memoria"]


def test_la_limpieza_es_lo_ultimo_y_tampoco_duerme(monkeypatch):
    b = Bucle(monkeypatch, ["limpieza"]).correr()
    assert b.hecho == ["pide", "mira set", "mira parecido", "mira limpieza"]
    assert b.esperas == []


def test_despues_de_cada_cola_secundaria_vuelve_a_pedir_temas_primero(monkeypatch):
    # Un tema sin analizar bloquea más que un set sin renderizar: tras cada trabajo secundario
    # se pregunta otra vez por análisis antes de seguir con la misma cola.
    b = Bucle(monkeypatch, ["set", "analisis", "limpieza", "analisis"]).correr()
    assert [h for h in b.hecho if h in ("pide", "analiza")] == ["pide", "pide", "analiza", "pide", "pide", "analiza"]


@pytest.mark.parametrize("trabajo", ["analisis", "set", "parecido", "limpieza"])
def test_cualquier_trabajo_devuelve_la_espera_a_la_base(monkeypatch, trabajo):
    b = Bucle(monkeypatch, [None, None, None, trabajo, None, None]).correr()
    assert b.esperas == [5, 10, 20, 5, 10]


# ----------------------------------------------------------------------------- lo que recibe process_job
def test_process_job_recibe_las_tres_subidas(monkeypatch):
    subidas = {"rendition": {"url": "r"}, "master": {"url": "m"}, "parecido": {"url": "p"}}
    b = Bucle(monkeypatch, ["analisis"], subidas=subidas).correr()
    assert b.procesados == [({"id": "j", "track_id": "t"}, {"bpm": 124}, "http://audio/x",
                             {"url": "r"}, {"url": "m"}, {"url": "p"})]


@pytest.mark.parametrize("subidas", [None, {}, {"rendition": {"url": "r"}}])
def test_process_job_sin_subidas_recibe_none_y_no_se_cae(monkeypatch, subidas):
    b = Bucle(monkeypatch, ["analisis"], subidas=subidas).correr()
    (_job, _track, _url, rendition, master, parecido), = b.procesados
    assert rendition == (subidas or {}).get("rendition")
    assert master is None and parecido is None


# ----------------------------------------------------------------------------- la plataforma no responde
def test_si_la_plataforma_no_responde_espera_y_vuelve_a_pedir(monkeypatch, capsys):
    b = Bucle(monkeypatch, ["error", "error", "error", "analisis"]).correr()
    assert b.hecho == ["pide", "espera", "pide", "espera", "pide", "espera", "pide", "analiza", "memoria"]
    for real, nominal in zip(b.esperas, [5, 10, 20]):    # crece, con ±20 % entre réplicas
        assert 0.8 * nominal <= real <= 1.2 * nominal
    assert capsys.readouterr().out.count("next_job error: la plataforma no responde") == 3


def test_con_la_plataforma_caida_no_mira_las_colas_secundarias(monkeypatch):
    b = Bucle(monkeypatch, ["error", "error"]).correr()
    assert not [h for h in b.hecho if h.startswith("mira")]


def test_tras_recuperarse_la_espera_vuelve_a_la_base(monkeypatch):
    b = Bucle(monkeypatch, ["error", "error", "analisis", None, None]).correr()
    assert b.esperas[2:] == [5, 10]


# ----------------------------------------------------------------------------- aviso de cola vacía
def test_avisa_que_no_hay_trabajo_una_vez_cada_doce_vueltas(monkeypatch, capsys):
    Bucle(monkeypatch, [None] * 25).correr()
    avisos = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("sin jobs pendientes")]
    assert len(avisos) == 3                                   # vueltas 1, 13 y 25
    assert avisos[0].endswith("(proxima consulta en 5 s)")    # dice cuánto va a esperar
    assert avisos[1].endswith("(proxima consulta en 120 s)")


def test_un_trabajo_reinicia_la_cuenta_del_aviso(monkeypatch, capsys):
    Bucle(monkeypatch, [None, None, "analisis", None, None]).correr()
    avisos = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("sin jobs pendientes")]
    assert len(avisos) == 2                                   # la primera vuelta vacía de cada racha


# ----------------------------------------------------------------------------- examen CM2 al arrancar
@pytest.mark.parametrize("resultado, aprobado", [(True, True), (False, False), (None, False), ({"ok": 1}, True)])
def test_el_examen_decide_si_cm2_queda_aprobado(monkeypatch, resultado, aprobado):
    b = Bucle(monkeypatch, ["analisis"])
    monkeypatch.setattr(worker, "GOLDEN_EXAM", True)
    monkeypatch.setattr(worker, "golden_exam", lambda: resultado)
    b.correr()
    assert worker.EXAMEN_CM2_APROBADO is aprobado
    assert b.hecho == ["memoria", "pide", "analiza", "memoria"]   # libera la RAM del examen antes de atender


def test_si_el_examen_revienta_el_worker_sigue_y_cm2_queda_sin_aprobar(monkeypatch, capsys):
    def examen():
        raise RuntimeError("sin golden set")

    b = Bucle(monkeypatch, ["analisis"])
    monkeypatch.setattr(worker, "GOLDEN_EXAM", True)
    monkeypatch.setattr(worker, "golden_exam", examen)
    monkeypatch.setattr(worker, "ENABLE_ANCHOR_BACKFILL", True)
    b.correr()
    assert worker.EXAMEN_CM2_APROBADO is False
    assert "analiza" in b.hecho                                   # siguió atendiendo temas
    assert "[CM2 EXAMEN] el examen fallo pero el worker sigue normal" in capsys.readouterr().out


def test_con_el_examen_apagado_no_se_corre_y_cm2_no_queda_aprobado(monkeypatch):
    corridas = []
    b = Bucle(monkeypatch, ["analisis"])
    monkeypatch.setattr(worker, "golden_exam", lambda: corridas.append(1) or True)
    b.correr()
    assert corridas == [] and worker.EXAMEN_CM2_APROBADO is False
    assert b.hecho == ["pide", "analiza", "memoria"]


def test_main_deja_puesto_el_manejador_de_sigterm(monkeypatch):
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    Bucle(monkeypatch, [None]).correr()
    assert signal.getsignal(signal.SIGTERM) is worker._al_apagar
