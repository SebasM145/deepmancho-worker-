"""Humo de la sombra del analizador v2: tiempo y memoria con 10 temas SINTÉTICOS.

Mide operación (contrato, tiempo, memoria), NO calidad: con 10 temas el margen es de ±25 puntos
(dj-connect `docs/plan/analizador-sombra.md` §2). No usa música de nadie: fabrica los temas.

Se corre DENTRO de la imagen del worker, con los límites que se quieran probar:

    docker build -t dmworker .
    docker run --rm --cpus 4 --memory 8g -v "$PWD/bancos:/bancos:ro" -v "$PWD/salida:/salida" \
        -e SOMBRA_V2=true -e SOMBRA_V2_HILOS=2 dmworker python /bancos/humo_sombra.py /salida/humo.json

Por cada tema corre lo mismo que `process_job` (análisis y sonoridad en procesos hijos) y después
la sombra, y anota los segundos de cada parte, el pico de memoria del proceso de la sombra y el
pico del contenedor entero (cgroup). Sin SOMBRA_V2=true mide solo el análisis de hoy: la
diferencia entre las dos corridas es lo que cuesta la sombra.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

import numpy as np

os.environ.setdefault("WORKER_API_URL", "http://humo.invalid")
os.environ.setdefault("WORKER_SECRET", "humo")
sys.path.insert(0, os.environ.get("WORKER_DIR", "/app"))
import worker  # noqa: E402

SR = 44100

# (nombre, bpm, minutos, tiempos por compás, con bombo, ancla en s)
TEMAS = [
    ("house_124_6min", 124.0, 6.0, 4, True, 0.06),
    ("techno_130_7min", 130.0, 7.0, 4, True, 0.25),
    ("deep_120_5min", 120.0, 5.0, 4, True, 0.50),
    ("tech_126_6min_intro_larga", 126.0, 6.0, 4, True, 15.30),
    ("rapido_150_4min", 150.0, 4.0, 4, True, 0.10),
    ("corto_128_2min", 128.0, 2.0, 4, True, 0.03),
    ("lento_95_3min", 95.0, 3.0, 4, True, 0.20),
    ("largo_125_10min", 125.0, 10.0, 4, True, 0.08),
    ("tres_cuartos_120_4min", 120.0, 4.0, 3, True, 0.10),
    ("sin_beats_4min", 0.0, 4.0, 4, False, 0.0),
]


def fabricar(bpm, minutos, por_compas, con_bombo, ancla_s, semilla):
    """Bombo, bajo, hats y un acorde, con secciones de 16 compases que entran y salen. Estéreo."""
    rng = np.random.default_rng(semilla)
    n = int(minutos * 60 * SR)
    t = np.arange(n) / SR
    y = np.zeros(n)
    if not con_bombo:   # pad y ruido filtrado: nada que parezca un beat
        for f in (110.0, 164.81, 220.0, 277.18):
            y += 0.15 * np.sin(2 * np.pi * f * t + rng.uniform(0, 6)) * (0.6 + 0.4 * np.sin(2 * np.pi * 0.05 * t))
        y += 0.02 * np.convolve(rng.standard_normal(n), np.ones(64) / 64, mode="same")
    else:
        beat = 60.0 / bpm
        compas = beat * por_compas
        tk = np.arange(int(0.18 * SR)) / SR
        bombo = np.sin(2 * np.pi * (48 + 90 * np.exp(-tk * 35)) * tk) * np.exp(-tk * 16)
        th = np.arange(int(0.03 * SR)) / SR
        k = 0
        while True:
            pos = ancla_s + k * beat
            i = int(pos * SR)
            if i >= n:
                break
            seccion = int((pos - ancla_s) // (16 * compas))
            lleno = seccion % 4 in (1, 2)             # 16 compases de entrada, 32 llenos, 16 de bajada
            m = min(len(bombo), n - i)
            y[i:i + m] += 0.9 * bombo[:m] * (1.15 if k % por_compas == 0 else 1.0)
            if lleno or seccion % 4 == 3:
                j = int((pos + beat / 2) * SR)
                m2 = min(len(th), max(0, n - j))
                y[j:j + m2] += 0.25 * rng.standard_normal(m2) * np.exp(-th[:m2] * 120)
            if lleno:
                m3 = min(int(beat * 0.9 * SR), n - i)
                tb = np.arange(m3) / SR
                y[i:i + m3] += 0.3 * np.sin(2 * np.pi * (55.0 if (k // por_compas) % 2 == 0 else 65.41) * tb) * np.exp(-tb * 3)
            k += 1
        for f in (220.0, 261.63, 329.63):
            y += 0.05 * np.sin(2 * np.pi * f * t)
    y = 0.85 * y / (np.max(np.abs(y)) + 1e-9)
    return np.stack([y, 0.97 * y], axis=1).astype(np.float32)


def a_mp3(audio, ruta):
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "2", "-i", "pipe:0",
                        "-codec:a", "libmp3lame", "-b:a", "320k", ruta], input=audio.tobytes(), check=False)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg no pudo escribir el mp3")


def pico_contenedor_mb():
    for ruta in ("/sys/fs/cgroup/memory.peak", "/sys/fs/cgroup/memory/memory.max_usage_in_bytes"):
        try:
            return round(int(open(ruta).read().strip()) / 1048576)
        except Exception:
            continue
    return None


def main():
    salida = sys.argv[1] if len(sys.argv) > 1 else "/tmp/humo.json"
    solo = set(sys.argv[2:])
    filas = []
    carpeta = tempfile.mkdtemp(prefix="humo_")
    # Como en producción: el worker corre su examen CM2 al arrancar, en el proceso padre (librosa y
    # numba ya cargados cuando llega el primer tema).
    if os.environ.get("HUMO_EXAMEN", "true").lower() != "false":
        try:
            worker.golden_exam()
        except Exception as e:  # noqa: BLE001
            print(f"examen CM2: {e}", flush=True)
    for semilla, (nombre, bpm, minutos, por_compas, con_bombo, ancla) in enumerate(TEMAS):
        if solo and nombre not in solo:
            continue
        ruta = os.path.join(carpeta, nombre + ".mp3")
        a_mp3(fabricar(bpm, minutos, por_compas, con_bombo, ancla, semilla), ruta)
        fila = {"tema": nombre, "bpm_real": bpm, "ancla_real_ms": round(ancla * 1000), "minutos": minutos}
        t0 = time.time()
        try:
            r = worker.en_proceso_aparte(worker.analyze, ruta, bpm_seed=None)
            r.update(worker.en_proceso_aparte(worker.medir_sonoridad, ruta))
            fila.update({"analisis_ok": True, "bpm": r.get("bpm_precise") or r.get("bpm"), "ancla_ms": r.get("first_beat_detected_ms"),
                         "cues": len(r.get("cue_points") or []), "alarmas": r.get("analysis_flags")})
        except Exception as e:  # noqa: BLE001
            fila.update({"analisis_ok": False, "error": f"{type(e).__name__}: {e}"[:200]})
        fila["analisis_s"] = round(time.time() - t0, 1)
        fila["pico_contenedor_tras_analisis_mb"] = pico_contenedor_mb()
        if worker.SOMBRA_V2:
            t1 = time.time()
            fila["v2"] = worker.sombra_v2(ruta, time.time())
            fila["sombra_s"] = round(time.time() - t1, 1)
            fila["pico_contenedor_tras_sombra_mb"] = pico_contenedor_mb()
        filas.append(fila)
        print(json.dumps(fila), flush=True)
        os.remove(ruta)
    resumen = {
        "sombra": worker.SOMBRA_V2, "hilos": worker.SOMBRA_V2_HILOS if worker.SOMBRA_V2 else None,
        "cpus_visibles": os.cpu_count(), "pico_contenedor_mb": pico_contenedor_mb(),
        "analisis_s_total": round(sum(f["analisis_s"] for f in filas), 1),
        "sombra_s_total": round(sum(f.get("sombra_s", 0) for f in filas), 1), "temas": filas,
    }
    with open(salida, "w") as f:
        json.dump(resumen, f)
    print(json.dumps({k: v for k, v in resumen.items() if k != "temas"}), flush=True)


if __name__ == "__main__":
    main()
