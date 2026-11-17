"""Analizador v2 del «1», EN SOMBRA (dj-connect #1211, `docs/plan/analizador-sombra.md`).

Calcula la rejilla con el detector nuevo (Beat This! `small0` + rejilla global + sesgo medido +
árbitro del «1») y devuelve un bloque `v2` APARTE. No decide nada: el worker lo manda dentro del
resultado y `worker-result` lo guarda en una columna propia, sin tocar `bpm`, `bpm_fine`,
`first_beat_offset_ms`, `grid_source` ni los cues. Pasar de sombra a real lo decide Germán con
los criterios de ese documento (§4).

Es el MISMO cálculo del banco de dj-connect (`tools/analizador/rejilla.ts`, `arbitro.ts` y
`sombra.ts`), llevado a Python. `tests/test_rejilla_v2.py` lo compara con bloques calculados
allá. Si cambia un lado, cambia el otro.

Lo puro (rejilla, árbitro, bloque) no necesita torch. El modelo se carga solo dentro de
`beats_del_modelo`, que el worker corre en un proceso NUEVO (`python rejilla_v2.py tema`).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import subprocess
import time

import numpy as np

# Versión del FORMATO del bloque (no del modelo): sube si cambia un campo.
V_BLOQUE = 1
SR_MODELO = 22050
MAX_S = 600            # lo mismo que analiza el worker (MAX_DURATION)
TOLERANCIA_MS = 70.0   # «1» correcto y contratiempo: a ≤ 70 ms del beat (convención del banco)

# Árbitro guardado por el banco: `tools/analizador/arbitros/small0.json` (2-nov-2026, 745 temas de
# `entrenar`). Copia exacta: con otro modelo u otros pesos, el hash de `version_de` cambia.
ARBITRO_SMALL0 = {
    "version": 1,
    "modelo": "small0",
    "regla": "logistica",
    "pesos": [2.067218820743347, 0.6993512284081471, -0.314701819006255, -0.3495037159150263,
              0.5048344529446284, 0.07107424144591487, 1.6035304092941192, -2.0986049599842747,
              1.059040649221862, 0.9612608580187828],
    "medias": [0.7398261840201351, 2.319939949966144, 11.489947349195381, 0.018466146192397632,
               0.14490566037735828, 125.49700066511528, 0.25471698113207547, 0.660377358490566,
               0.08490566037735849],
    "desvios": [0.17227004162746185, 0.11796426481438545, 6.499383670697325, 0.04283196502349343,
                0.2968362908285741, 2.8905346292081804, 0.43570200900964107, 0.4735811470947601,
                0.27874125854139936],
    "sesgoMs": -33.7,
    "temas": 745,
    "hashLista": "c7844f94af8028643dba3478ad12781539393d34fc180f7967ac02a48a639ced",
    "fecha": "2026-11-02",
}

BEATS_FASE = 64
MIN_TRAMO = 16


def _redondeo_js(x: float) -> int:
    """Math.round de JavaScript (,5 hacia arriba): el banco redondea así los índices de beat."""
    return int(math.floor(x + 0.5))


def mediana(xs) -> float:
    v = sorted(xs)
    k = len(v) // 2
    return v[k] if len(v) % 2 else (v[k - 1] + v[k]) / 2


def _pendiente_tramo(t, ini: int, n: int) -> float:
    k_medio = (n - 1) / 2
    t_medio = sum(t[ini:ini + n]) / n
    num = den = 0.0
    for j in range(n):
        num += (j - k_medio) * (t[ini + j] - t_medio)
        den += (j - k_medio) ** 2
    return num / den


def _mediana_ponderada(valores, pesos) -> float:
    orden = sorted(zip(valores, pesos), key=lambda p: p[0])
    total = sum(pesos)
    acum = 0
    for v, w in orden:
        acum += w
        if acum * 2 >= total:
            return v
    return orden[-1][0]


def rejilla_global(beats, downbeats, beats_fase: int = BEATS_FASE):
    """Una sola recta (BPM + fase) ajustada a todos los beats, y el «1» por voto de los downbeats.
    `beats` y `downbeats` en segundos. None si no hay con qué. Port de `rejillaGlobal` (rejilla.ts)."""
    t = sorted(float(x) for x in beats if x is not None and math.isfinite(float(x)))
    if len(t) < 8:
        return None
    periodo0 = mediana([t[i] - t[i - 1] for i in range(1, len(t))])
    if not (0.15 < periodo0 < 2):
        return None  # 30–400 BPM

    # Período fino: pendiente de cada tramo consecutivo sin huecos ni dobles tiempos.
    pendientes, pesos = [], []
    ini = 0
    for i in range(1, len(t) + 1):
        corte = i == len(t) or abs(t[i] - t[i - 1] - periodo0) > periodo0 * 0.1
        if not corte:
            continue
        n = i - ini
        if n >= MIN_TRAMO:
            pendientes.append(_pendiente_tramo(t, ini, n))
            pesos.append(n)
        ini = i
    b = _mediana_ponderada(pendientes, pesos) if pendientes else periodo0

    # Índices contra la recta que pasa por el primer beat; fase = mediana de los primeros beats.
    a0 = t[0]
    ks, ts = [], []
    for ti in t:
        k = _redondeo_js((ti - a0) / b)
        if abs(ti - (a0 + b * k)) <= b * 0.25:
            ks.append(k)
            ts.append(ti)
    if len(ks) < 8:
        return None
    a = mediana([ts[i] - b * k for i, k in enumerate(ks[:beats_fase])])
    residuo = math.sqrt(sum((ts[i] - (a + b * ks[i])) ** 2 for i in range(len(ks))) / len(ks))

    # Voto de los downbeats: ¿en qué posición (0..3) de la rejilla caen?
    votos = [0, 0, 0, 0]
    for d in downbeats:
        d = float(d)
        k = _redondeo_js((d - a) / b)
        if abs(d - (a + b * k)) > b * 0.25:
            continue
        votos[k % 4] += 1
    total = sum(votos)
    fase_uno = votos.index(max(votos)) if total else 0
    acuerdo = votos[fase_uno] / total if total else 0.0

    def primer_uno_ms(fase: int) -> float:
        k0 = fase
        while a + b * k0 < 0:
            k0 += 4
        while a + b * (k0 - 4) >= 0:
            k0 -= 4
        return (a + b * k0) * 1000

    return {
        "bpm": 60 / b,
        "ancla_ms": primer_uno_ms(fase_uno),
        "beat_ms": b * 1000,
        "fase_uno": fase_uno,
        "acuerdo_uno": acuerdo,
        "votos": votos,
        "total_votos": total,
        "ancla_primer_beat_ms": primer_uno_ms(ks[0] % 4),
        "primer_beat_s": t[0],
        "n_beats": len(ks),
        "descartados": len(t) - len(ks),
        "residuo_ms": residuo * 1000,
    }


def vector_rasgos(r: dict) -> list:
    """Lo que el árbitro sabe de un tema (`rasgosDe` + `vectorRasgos` de arbitro.ts)."""
    total = r["n_beats"] + r["descartados"]
    return [
        r["acuerdo_uno"], math.log10(1 + r["total_votos"]), r["residuo_ms"],
        r["descartados"] / total if total else 0.0, min(r["primer_beat_s"], 60), r["bpm"],
        1 if r["fase_uno"] == 1 else 0, 1 if r["fase_uno"] == 2 else 0, 1 if r["fase_uno"] == 3 else 0,
    ]


def puntaje(r: dict, arbitro: dict = ARBITRO_SMALL0) -> float:
    """Regresión logística guardada: > 0 gana el voto de los downbeats; < 0, «el primer beat es el 1»."""
    d = len(arbitro["medias"])
    z = [(v - arbitro["medias"][j]) / arbitro["desvios"][j] for j, v in enumerate(vector_rasgos(r))]
    return arbitro["pesos"][d] + sum(v * arbitro["pesos"][j] for j, v in enumerate(z))


def version_de(arbitro: dict = ARBITRO_SMALL0) -> str:
    """El mismo hash corto que `versionDe` (sombra.ts): modelo + regla + pesos + sesgo. Dos bloques
    con la misma versión se calcularon igual, aquí o en el banco."""
    datos = {"m": arbitro["modelo"], "r": arbitro["regla"]}
    if arbitro.get("umbral") is not None:
        datos["u"] = arbitro["umbral"]
    datos["p"] = arbitro["pesos"]
    datos["s"] = arbitro["sesgoMs"]
    return hashlib.sha256(json.dumps(datos, separators=(",", ":")).encode()).hexdigest()[:12]


def _r(x: float, n: int) -> float:
    """Math.round(x·10ⁿ)/10ⁿ, como el banco."""
    f = 10 ** n
    return _redondeo_js(x * f) / f


def bpm_redondo(bpm: float, tolerancia: float = 0.1):
    """R2: el BPM entero cuando la medida cae a ≤ 0,1 del entero (97 % de las rejillas de Rekordbox
    del ejemplo son enteras). None si no cae: la sombra guarda las dos cosas para medir la regla."""
    e = _redondeo_js(bpm)
    return e if abs(bpm - e) <= tolerancia else None


def bloque_v2(r: dict, arbitro: dict = ARBITRO_SMALL0) -> dict:
    """El bloque que viaja en el resultado. Los campos del plan (`bpm`, `ancla_ms`, `regla`,
    `acuerdo`, `confianza`, `intro_s`, `version`) salen igual que en `bloqueV2` (sombra.ts); el
    resto son las medidas con las que decide el árbitro, para diseñar después una señal de duda
    (la confianza de hoy no separa aciertos de errores: auditoría del 15-nov, §5.2)."""
    p = puntaje(r, arbitro)
    voto = p >= 0
    ancla = (r["ancla_ms"] if voto else r["ancla_primer_beat_ms"]) - arbitro["sesgoMs"]
    total = r["n_beats"] + r["descartados"]
    return {
        "v": V_BLOQUE,
        "bpm": _r(r["bpm"], 2),
        "bpm_fino": _r(r["bpm"], 3),
        "bpm_redondo": bpm_redondo(r["bpm"]),
        "ancla_ms": _r(ancla, 1),
        "regla": "voto" if voto else "primer_beat",
        "acuerdo": _r(r["acuerdo_uno"], 2),
        "confianza": _r(1 / (1 + math.exp(-abs(p))), 2),
        "intro_s": _r(r["primer_beat_s"], 2),
        "version": version_de(arbitro),
        "modelo": arbitro["modelo"],
        "fase_uno": r["fase_uno"],
        "votos": list(r["votos"]),
        "n_beats": r["n_beats"],
        "descartados": _r(r["descartados"] / total if total else 0.0, 3),
        "residuo_ms": _r(r["residuo_ms"], 1),
    }


# ── Modelo (torch): solo dentro del proceso hijo ────────────────────────────────────────────────
# Pesos de Beat This! `small0` (CPJKU, ISMIR 2024; código MIT). Los mismos con los que midió el
# banco. Se bajan UNA vez al construir la imagen (Dockerfile) y se comprueban por sha256: en
# marcha el worker nunca baja nada, y con otros pesos la sombra no corre.
PESOS_URL = "https://cloud.cp.jku.at/public.php/dav/files/7ik4RrBKTS273gp/small0.ckpt"
PESOS_SHA256 = "6074be2c4d490c5f6101fcc374a1ec72ae93456e23bb6019783b849f5dc7d47b"


def ruta_pesos() -> str:
    """Donde torch guarda sus descargas (TORCH_HOME o ~/.cache/torch), sin importar torch."""
    base = os.environ.get("TORCH_HOME") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache"), "torch")
    return os.path.join(base, "hub", "checkpoints", "beat_this-small0.ckpt")


def _sha256(ruta: str) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for trozo in iter(lambda: f.read(1 << 20), b""):
            h.update(trozo)
    return h.hexdigest()


def bajar_pesos() -> bool:
    """Para el Dockerfile. Baja los pesos y los comprueba. Si la descarga falla (red), avisa y
    devuelve False: la imagen sale igual y la sombra queda apagada. Si lo bajado NO es el archivo
    esperado, lanza: eso no se deja pasar."""
    import urllib.request
    ruta = ruta_pesos()
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    if not os.path.exists(ruta):
        try:
            urllib.request.urlretrieve(PESOS_URL, ruta + ".tmp")
            os.replace(ruta + ".tmp", ruta)
        except Exception as e:  # noqa: BLE001
            print(f"AVISO: no se pudieron bajar los pesos del detector v2 ({type(e).__name__}): la sombra queda apagada")
            return False
    if _sha256(ruta) != PESOS_SHA256:
        os.remove(ruta)
        raise RuntimeError("los pesos del detector v2 no son los esperados (sha256 distinto)")
    print(f"pesos del detector v2 listos en {ruta}")
    return True


def disponible() -> bool:
    """¿Están instalados el modelo y sus pesos? Sin importar torch (carga cientos de MB)."""
    return all(importlib.util.find_spec(m) is not None for m in ("torch", "beat_this")) and os.path.exists(ruta_pesos())


def decodificar(path: str, sr: int = SR_MODELO, max_s: int = MAX_S) -> np.ndarray:
    """Mono, 22 050 Hz, float32, con ffmpeg (como `tools/analizador/py/inferir.py`)."""
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-t", str(max_s), "-i", path, "-f", "f32le", "-ac", "1", "-ar", str(sr), "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("ffmpeg no pudo decodificar el audio para el detector v2")
    return np.frombuffer(r.stdout, dtype=np.float32)


def beats_del_modelo(path: str, modelo: str = "small0", hilos: int = 2):
    """Beats y downbeats (s) de Beat This! en CPU, y la duración analizada (s)."""
    import torch
    torch.set_num_threads(max(1, int(hilos)))
    from beat_this.inference import Audio2Beats
    senal = decodificar(path)
    if modelo != "small0":
        raise RuntimeError(f"la sombra solo trae los pesos de small0, no de {modelo}")
    a2b = Audio2Beats(checkpoint_path=ruta_pesos(), device="cpu", dbn=False)   # archivo local: sin red
    with torch.inference_mode():
        beats, downbeats = a2b(senal, SR_MODELO)
    return [float(x) for x in beats], [float(x) for x in downbeats], len(senal) / SR_MODELO


def _pico_mb() -> float:
    """Pico de memoria residente de ESTE proceso (MB). Se lee VmHWM de /proc: `ru_maxrss` no sirve
    en un proceso recién lanzado, porque en Linux arranca con el pico de quien lo lanzó (medido
    el 17-nov: daba 1.111 MB, lo que ocupaba el worker, en todos los temas)."""
    try:
        with open("/proc/self/status") as f:
            for linea in f:
                if linea.startswith("VmHWM:"):
                    return round(int(linea.split()[1]) / 1024.0, 0)
    except Exception:
        pass
    import resource
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 0)


def sombra(path: str, hilos: int = 2, arbitro: dict = ARBITRO_SMALL0) -> dict:
    """Todo el cálculo de la sombra para un archivo. Nunca lanza por un tema sin beats: devuelve
    un bloque con `error`, que también es un dato de operación (cuántos fallan y por qué)."""
    t0 = time.time()
    base = {"v": V_BLOQUE, "version": version_de(arbitro), "modelo": arbitro["modelo"]}
    beats, downbeats, dur_s = beats_del_modelo(path, arbitro["modelo"], hilos)
    r = rejilla_global(beats, downbeats)
    if r is None:
        bloque = {**base, "error": "sin_rejilla", "n_beats": len(beats)}
    else:
        bloque = bloque_v2(r, arbitro)
    bloque["seg"] = round(time.time() - t0, 1)
    bloque["mem_mb"] = _pico_mb()
    bloque["dur_s"] = round(dur_s, 1)
    bloque["hilos"] = int(hilos)
    return bloque


if __name__ == "__main__":
    # `python rejilla_v2.py tema.mp3` → una línea JSON con el bloque. Así lo llama el worker
    # (`correr_rejilla_v2`): en un proceso nuevo, para que torch no herede nada del análisis.
    import sys
    print(json.dumps(sombra(sys.argv[1], int(os.environ.get("SOMBRA_V2_HILOS", "2")))), flush=True)
