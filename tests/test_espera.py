"""W2 (30-sep-2026): espera creciente con la cola vacía. Meta: < 5.000 consultas
al día sin trabajo (antes ~73.000)."""
import os
import sys

import pytest

os.environ.setdefault("WORKER_API_URL", "http://prueba.invalid")
os.environ.setdefault("WORKER_SECRET", "prueba")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import grid_verifier  # noqa: E402
import stems_worker  # noqa: E402
import worker  # noqa: E402

MODULOS = [worker, stems_worker, grid_verifier]


class Basta(Exception):
    """Corta el bucle infinito de main() en la prueba."""


def reloj(vueltas):
    esperas = []

    def sleep(s):
        esperas.append(s)
        if len(esperas) >= vueltas:
            raise Basta

    return esperas, sleep


@pytest.mark.parametrize("mod", MODULOS)
def test_secuencia_y_reinicio(mod):
    e = mod.Espera(5, 120)
    assert [e.vacia() for _ in range(8)] == [5, 10, 20, 40, 80, 120, 120, 120]
    e.trabajo()
    assert e.vacia() == 5


@pytest.mark.parametrize("mod", MODULOS)
def test_error_crece_con_variacion(mod):
    e = mod.Espera(10, 60)
    s = [e.error() for _ in range(5)]
    for real, nominal in zip(s, [10, 20, 40, 60, 60]):
        assert 0.8 * nominal <= real <= 1.2 * nominal


def test_worker_espera_mas_con_la_cola_vacia(monkeypatch):
    esperas, sleep = reloj(7)
    monkeypatch.setattr(worker, "GOLDEN_EXAM", False)
    monkeypatch.setattr(worker, "next_job", lambda: (None, None, None, None))
    monkeypatch.setattr(worker.time, "sleep", sleep)
    with pytest.raises(Basta):
        worker.main()
    assert esperas == [5, 10, 20, 40, 80, 120, 120]


def test_worker_vuelve_a_la_base_con_trabajo(monkeypatch):
    colas = iter([None] * 4 + ["job"] + [None] * 10)

    def next_job():
        return ({"id": "j", "track_id": "t"}, {}, None, {}) if next(colas) else (None, None, None, None)

    esperas, sleep = reloj(6)
    monkeypatch.setattr(worker, "GOLDEN_EXAM", False)
    monkeypatch.setattr(worker, "next_job", next_job)
    monkeypatch.setattr(worker, "process_job", lambda *a: None)
    monkeypatch.setattr(worker.time, "sleep", sleep)
    with pytest.raises(Basta):
        worker.main()
    assert esperas == [5, 10, 20, 40, 5, 10]


def test_stems_espera_mas_con_las_tres_colas_vacias(monkeypatch):
    llamadas = []
    for f in ("claim_render", "claim", "claim_loops"):
        monkeypatch.setattr(stems_worker, f, lambda f=f: llamadas.append(f))
    esperas, sleep = reloj(4)
    monkeypatch.setattr(stems_worker, "POLL", 15)
    monkeypatch.setattr(stems_worker.time, "sleep", sleep)
    with pytest.raises(Basta):
        stems_worker.main()
    assert esperas == [15, 30, 60, 120]
    assert len(llamadas) == 3 * 4  # render, stems y loops en cada vuelta


def test_verificador_espera_mas_con_la_cola_vacia(monkeypatch):
    segundos = []

    def sleep(s):
        segundos.append(s)
        if len(segundos) >= 5 + 10 + 20 + 40:
            raise Basta

    monkeypatch.setattr(grid_verifier, "API", "http://prueba.invalid")
    monkeypatch.setattr(grid_verifier, "SECRET", "prueba")
    monkeypatch.setattr(grid_verifier, "process_one", lambda: False)
    monkeypatch.setattr(grid_verifier.time, "sleep", sleep)
    monkeypatch.setattr(grid_verifier, "POLL_S", 5)
    with pytest.raises(Basta):
        grid_verifier.main_loop()
    assert len(segundos) == 75  # 5 + 10 + 20 + 40 esperas de 1 s (sale rápido con SIGTERM)


def test_consultas_al_dia_con_la_cola_vacia():
    """Régimen estable (tope): 3 réplicas de análisis, stems con 3 consultas por
    vuelta y el verificador. Con W1 (1 réplica) baja todavía más."""
    dia = 86400
    analisis = 3 * dia / worker.POLL_MAX
    stems = 3 * dia / stems_worker.POLL_MAX
    verificador = dia / grid_verifier.POLL_MAX_S
    assert analisis + stems + verificador < 5000
