"""7.6.25 · La sombra del analizador v2 dentro del trabajo: va APARTE, apagada de fábrica, y nunca
tumba ni cambia el análisis."""
import time

import pytest

import rejilla_v2
import worker
from test_process_job_contrato import ANALISIS, correr, job, resultado  # noqa: F401  (job es el fixture)

BLOQUE = {"v": 1, "bpm": 124.0, "bpm_fino": 124.002, "bpm_redondo": 124, "ancla_ms": 153.7, "regla": "voto",
          "acuerdo": 0.9, "confianza": 0.97, "intro_s": 0.12, "version": rejilla_v2.version_de(), "modelo": "small0",
          "seg": 30.0, "mem_mb": 600.0, "dur_s": 300.0, "hilos": 2}


@pytest.fixture
def sombra(monkeypatch):
    """El proceso del modelo, simulado: `correr_rejilla_v2` devuelve lo que diga `estado`."""
    estado = {"llamadas": [], "resultado": dict(BLOQUE)}

    def falsa(path, hilos, tope_s):
        estado["llamadas"].append((path, hilos))
        estado["tope"] = tope_s
        if isinstance(estado["resultado"], BaseException):
            raise estado["resultado"]
        return dict(estado["resultado"]) if isinstance(estado["resultado"], dict) else estado["resultado"]

    monkeypatch.setattr(worker, "SOMBRA_V2", True)
    monkeypatch.setattr(rejilla_v2, "disponible", lambda: True)
    monkeypatch.setattr(worker, "correr_rejilla_v2", falsa)
    return estado


def test_de_fabrica_no_se_calcula_si_la_plataforma_no_lo_pide(job, monkeypatch):
    assert worker.SOMBRA_V2 is False and worker.SOMBRA_V2_FRENO is False
    monkeypatch.setattr(rejilla_v2, "disponible", lambda: True)
    monkeypatch.setattr(worker, "correr_rejilla_v2", lambda *a, **k: pytest.fail("no debía calcular"))
    correr()
    assert "v2" not in resultado(job)


def test_si_la_plataforma_lo_pide_se_calcula_sin_variable_en_railway(job, monkeypatch):
    monkeypatch.setattr(rejilla_v2, "disponible", lambda: True)
    monkeypatch.setattr(worker, "correr_rejilla_v2", lambda path, hilos, tope_s: dict(BLOQUE))
    correr(pedir_sombra=True)
    assert resultado(job)["v2"] == BLOQUE


def test_el_freno_de_mano_gana_aunque_la_plataforma_lo_pida(job, sombra, monkeypatch):
    monkeypatch.setattr(worker, "SOMBRA_V2_FRENO", True)
    correr(pedir_sombra=True)
    assert "v2" not in resultado(job) and sombra["llamadas"] == []


def test_next_job_lee_el_pedido_de_la_plataforma(monkeypatch):
    class R:
        status_code = 200

        def __init__(self, cuerpo):
            self._c = cuerpo

        def raise_for_status(self):
            pass

        def json(self):
            return self._c

    vistos = {}

    def post(url, headers=None, timeout=None):
        vistos["cap"] = headers["x-worker-capacidades"]
        return R(vistos["cuerpo"])

    monkeypatch.setattr(worker.requests, "post", post)
    for cuerpo, esperado in (({"job": {"id": "j"}, "sombra_v2": True}, True), ({"job": {"id": "j"}}, False),
                             ({"job": {"id": "j"}, "sombra_v2": "true"}, False)):
        vistos["cuerpo"] = cuerpo
        assert worker.next_job()[3]["sombra_v2"] is esperado
    assert vistos["cap"] == worker.CAPACIDADES and "parecido-1" in vistos["cap"].split(",")


def test_la_capacidad_solo_se_anuncia_con_el_modelo_instalado():
    assert ("sombra-v2-1" in worker.CAPACIDADES.split(",")) == (worker.sombra_instalada() and not worker.SOMBRA_V2_FRENO)


def test_encendida_el_bloque_va_aparte_y_no_cambia_nada_mas(job, sombra):
    correr()
    con = resultado(job)
    assert con["v2"] == BLOQUE and sombra["llamadas"] == [(str(job["audio"]), worker.SOMBRA_V2_HILOS)]
    # Lo demás es idéntico a un trabajo sin sombra: ni bpm, ni ancla, ni cues, ni tono.
    sin_v2 = {k: v for k, v in con.items() if k != "v2"}
    for k, v in ANALISIS.items():
        assert sin_v2[k] == v, k
    assert sin_v2["key_detected"] == "8A"


def test_sin_el_modelo_instalado_no_manda_nada_y_el_analisis_sigue(job, sombra, monkeypatch, capsys):
    monkeypatch.setattr(rejilla_v2, "disponible", lambda: False)
    monkeypatch.setattr(worker, "_AVISO_SOMBRA", False)
    correr()
    assert "v2" not in resultado(job) and sombra["llamadas"] == []
    assert "el modelo no está instalado" in capsys.readouterr().out


def test_si_el_modelo_falla_el_analisis_se_entrega_igual_con_el_error_anotado(job, sombra):
    sombra["resultado"] = RuntimeError("torch explotó con https://x.supabase.co/storage/v1/object/sign/music/a.mp3?token=abc")
    correr()
    r = resultado(job)
    assert r["bpm"] == 124.02
    assert r["v2"] == {"v": 1, "version": rejilla_v2.version_de(), "modelo": "small0", "error": "fallo"}


def test_sin_tiempo_dentro_del_tope_no_arranca_el_modelo(job, sombra, monkeypatch):
    monkeypatch.setattr(worker, "TOPE_TRABAJO_S", 30)       # menos que la reserva + 20 s
    correr()
    assert resultado(job)["v2"]["error"] == "sin_tiempo" and sombra["llamadas"] == []


def test_la_sombra_es_lo_ultimo_despues_de_la_copia_de_escucha(job, sombra):
    orden = []
    viejo_rend = worker.make_rendition
    viejo = worker.correr_rejilla_v2
    worker.make_rendition = lambda *a, **k: (orden.append("copia"), viejo_rend(*a, **k))[1]
    worker.correr_rejilla_v2 = lambda *a, **k: (orden.append("sombra"), viejo(*a, **k))[1]
    try:
        correr(rendition_upload={"url": "http://x/subir", "path": "dj/streams/t.mp3"})
    finally:
        worker.make_rendition = viejo_rend
        worker.correr_rejilla_v2 = viejo
    assert orden == ["copia", "sombra"]


def test_el_tope_del_modelo_deja_un_bloque_con_error_y_no_tumba_el_trabajo(job, sombra, monkeypatch):
    import subprocess
    monkeypatch.setattr(worker, "SOMBRA_V2_TOPE_S", 20)
    sombra["resultado"] = subprocess.TimeoutExpired("python rejilla_v2.py", 20)
    correr()
    r = resultado(job)
    assert r["bpm"] == 124.02 and sombra["tope"] == 20
    assert r["v2"] == {"v": 1, "version": rejilla_v2.version_de(), "modelo": "small0", "error": "tope", "seg": 20.0}


@pytest.mark.parametrize("devuelve,error", [("caido", "caido"), (None, "fallo"), ({"v": 9}, "fallo")])
def test_si_el_proceso_del_modelo_muere_o_responde_mal_queda_el_error(job, sombra, devuelve, error):
    sombra["resultado"] = devuelve
    correr()
    r = resultado(job)
    assert r["bpm"] == 124.02 and r["v2"]["error"] == error


# ── el proceso del modelo, de verdad (con un rejilla_v2.py de mentira: sin torch) ─────────────
def _modulo_falso(tmp_path, monkeypatch, cuerpo):
    falso = tmp_path / "rejilla_v2.py"
    falso.write_text(cuerpo)
    monkeypatch.setattr(rejilla_v2, "__file__", str(falso))


def test_correr_rejilla_v2_lee_la_ultima_linea_json_del_proceso(tmp_path, monkeypatch):
    _modulo_falso(tmp_path, monkeypatch, 'import os, sys, json\nprint("aviso de una libreria")\n'
                  'print(json.dumps({"v": 1, "tema": sys.argv[1], "hilos": os.environ["SOMBRA_V2_HILOS"]}))\n')
    assert worker.correr_rejilla_v2("/tmp/x.mp3", 3, 30) == {"v": 1, "tema": "/tmp/x.mp3", "hilos": "3"}


def test_correr_rejilla_v2_corta_al_pasar_el_tope(tmp_path, monkeypatch):
    import subprocess
    _modulo_falso(tmp_path, monkeypatch, "import time\ntime.sleep(60)\n")
    t0 = time.time()
    with pytest.raises(subprocess.TimeoutExpired):
        worker.correr_rejilla_v2("/tmp/x.mp3", 2, 1)
    assert time.time() - t0 < 15


def test_correr_rejilla_v2_distingue_caida_de_fallo(tmp_path, monkeypatch):
    _modulo_falso(tmp_path, monkeypatch, "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n")
    assert worker.correr_rejilla_v2("/tmp/x.mp3", 2, 30) == "caido"
    _modulo_falso(tmp_path, monkeypatch, "raise SystemExit(3)\n")
    assert worker.correr_rejilla_v2("/tmp/x.mp3", 2, 30) is None
