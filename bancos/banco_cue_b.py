"""Banco del cue B (dj-connect #1064): ¿B cae donde entra el bajo?

Usa la cache del banco del cue A (`bancos/banco_cue_a.py`: audio a SR del análisis, BPM fino y
ancla, un .npz por TrackID del XML de Rekordbox) y mide, por tema, el B de antes (primer cambio
de estructura) y el de ahora (`worker.entrada_del_bajo`):
  · dos bajos: compases ANTES de B con la energía < 150 Hz a nivel de cuerpo (RMS a <= 6 dB del
    percentil 75 del tema, el criterio de la auditoría del 17-oct). Cuenta también el bombo
    solo, así que es una cota alta; lo que importa es el antes frente al después.
  · B en borde de frase (múltiplo de 4 y de 8 compases desde el primer downbeat).
  · distancia al cue B de Mixed In Key (Num=1 del XML), solo como referencia: MIK no marca la
    entrada del bajo sino un cambio de energía.
Solo lee: no escribe en la base ni sube nada. El audio y el XML son de Germán y no entran al repo.

Uso:
  python bancos/banco_cue_b.py <rekordbox.xml> <carpeta cache> [salida.jsonl]
"""
import json
import math
import os
import statistics as st
import sys
import urllib.parse
import xml.etree.ElementTree as ET

os.environ.setdefault("WORKER_API_URL", "http://banco.invalid")
os.environ.setdefault("WORKER_SECRET", "banco")
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

import numpy as np  # noqa: E402

import worker  # noqa: E402


def cues_b_de_mik(xml):
    """TrackID -> segundo del cue B de MIK (o None), para los temas con audio en este equipo."""
    out = {}
    for t in ET.parse(xml).getroot().iter("TRACK"):
        loc = t.get("Location")
        if not loc or not os.path.exists(urllib.parse.unquote(loc.replace("file://localhost", ""))):
            continue
        b = [float(p.get("Start")) for p in t.findall("POSITION_MARK") if p.get("Num") == "1"]
        out[t.get("TrackID")] = b[0] if b else None
    return out


def rms_graves_por_compas(y, sr, origen_ms, bar_ms, n_bars):
    from scipy.signal import butter, sosfiltfilt
    g = sosfiltfilt(butter(4, [30 / (sr / 2), 150 / (sr / 2)], btype="band", output="sos"), y.astype(np.float64))
    e = np.empty(n_bars)
    for i in range(n_bars):
        seg = g[int((origen_ms + i * bar_ms) * sr / 1000):int((origen_ms + (i + 1) * bar_ms) * sr / 1000)]
        e[i] = 20 * np.log10(max(float(np.sqrt(np.mean(seg ** 2))) if seg.size else 0.0, 1e-6))
    return e


def b_de(cues):
    return next((c for c in cues or [] if c["number"] == 1), None)


def medir_tema(ruta_npz, mik_b_s):
    d = np.load(ruta_npz)
    y, bpm, fb = d["y"], float(d["bpm"]), float(d["fb"])
    sr = worker.SR
    bar_ms = 240000.0 / bpm
    origen = max(0.0, fb - math.floor(fb / bar_ms + 1e-6) * bar_ms)
    n_bars = int((len(y) / sr * 1000 - origen) // bar_ms)
    real = worker.entrada_del_bajo
    worker.entrada_del_bajo = lambda *a, **k: None
    try:
        antes = b_de(worker.detect_cues(y, sr, bpm, fb))
    finally:
        worker.entrada_del_bajo = real
    ahora = b_de(worker.detect_cues(y, sr, bpm, fb))
    graves = rms_graves_por_compas(y, sr, origen, bar_ms, n_bars)
    cuerpo = float(np.percentile(graves, 75)) if n_bars else 0.0
    fila = {"id": os.path.basename(ruta_npz)[:-4], "bpm": round(bpm, 2), "compases": n_bars}
    for nombre, b in (("antes", antes), ("ahora", ahora)):
        if not b:
            fila[nombre] = None
            continue
        k = int(round((b["positionMs"] - origen) / bar_ms))
        fila[nombre] = {"compas": k, "conf": b["confidence"], "medido": b.get("origen") == "auto",
                        "dos_bajos": int(sum(graves[:k] >= cuerpo - 6)),
                        "mik_s": None if mik_b_s is None else round(b["positionMs"] / 1000 - mik_b_s, 2)}
    return fila


def resumen(filas):
    ok = [f for f in filas if f.get("antes") and f.get("ahora")]
    n = len(ok)
    pct = lambda k: f"{k} de {n} ({100 * k / n:.0f} %)" if n else "0"  # noqa: E731
    medidos = [f for f in ok if f["ahora"]["medido"]]
    print(f"temas {len(filas)} · con cues del plan A {n} · B medido en la entrada del bajo {pct(len(medidos))}")
    movidos = [f["ahora"]["compas"] - f["antes"]["compas"] for f in medidos if f["ahora"]["compas"] != f["antes"]["compas"]]
    if movidos:
        print(f"B se movio en {pct(len(movidos))}: antes {sum(m < 0 for m in movidos)} · despues "
              f"{sum(m > 0 for m in movidos)} · mediana {st.median(movidos):+.0f} compases")
    for v in ("antes", "ahora"):
        dos = [f[v]["dos_bajos"] for f in ok]
        print(f"{v}: temas con >= 2 compases de graves a nivel de cuerpo antes de B {pct(sum(x >= 2 for x in dos))}"
              f" · B en múltiplo de 8 {pct(sum(f[v]['compas'] % 8 == 0 for f in ok))}"
              f" · de 4 {pct(sum(f[v]['compas'] % 4 == 0 for f in ok))}")
        mik = [(abs(f[v]["mik_s"]), 240 / f["bpm"]) for f in ok if f[v]["mik_s"] is not None]
        if mik:
            print(f"   |B − B de MIK| mediana {st.median(x for x, _ in mik):.1f} s · <= 1 compás "
                  f"{sum(x <= bar for x, bar in mik) * 100 / len(mik):.0f} % (de {len(mik)} con B de MIK)")


if __name__ == "__main__":
    xml, cache = sys.argv[1], sys.argv[2]
    salida = sys.argv[3] if len(sys.argv) > 3 else None
    mik = cues_b_de_mik(xml)
    filas = []
    for nombre in sorted(os.listdir(cache)):
        if not nombre.endswith(".npz"):
            continue
        tid = nombre[:-4]
        try:
            filas.append(medir_tema(os.path.join(cache, nombre), mik.get(tid)))
        except Exception as e:  # noqa: BLE001 - un tema roto no para el banco
            filas.append({"id": tid, "error": repr(e)[:200]})
        if len(filas) % 25 == 0:
            print(f"… {len(filas)}", flush=True)
    if salida:
        with open(salida, "w") as f:
            for x in filas:
                f.write(json.dumps(x) + "\n")
    resumen(filas)
