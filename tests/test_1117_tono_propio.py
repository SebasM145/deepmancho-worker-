"""7.6.25 · T1: el tono que midió el worker viaja siempre en `key_detected`, sin tocar `key`."""
from test_process_job_contrato import correr, job, resultado  # noqa: F401  (job es el fixture)


def test_con_etiqueta_el_tono_propio_viaja_aparte_y_key_no_se_manda(job):
    correr({"key": "5A", "artist": "A", "title": "B"})
    r = resultado(job)
    assert r["key"] is None            # la etiqueta no se pisa (regla de siempre)
    assert r["key_detected"] == "8A"   # pero lo que medimos ya no se bota


def test_sin_etiqueta_van_los_dos(job):
    correr()
    r = resultado(job)
    assert r["key"] == "8A" and r["key_detected"] == "8A"


def test_si_el_detector_no_dio_tono_va_vacio(job):
    job["analisis"] = {**job["analisis"], "key": None}
    correr()
    assert resultado(job)["key_detected"] is None
