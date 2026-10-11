"""Banco de hot cues y energía contra Mixed In Key (dj-connect #860).

Corre el MISMO camino de analyze() que decide los cues y la energía del tema (BPM fino,
ancla, detect_cues o plan B, cuantizar_cues, compute_energy) sobre los 50 temas del banco
`docs/hot-cues/referencia-mik-piso3-2026-10-09.json` y deja un JSON que mide
`scripts/medir/cues-mik.mjs` en dj-connect. No escribe en la base ni sube nada.

Uso:
  python bancos/banco_mik.py --banco banco.json --audio <carpeta con <track_id>.mp3|.flac> \
      --cache <carpeta> --salida resultado.json

La parte cara (cargar el audio, BPM fino y ancla) se guarda en --cache: al cambiar el
detector, la segunda corrida tarda segundos. El audio es de Germán y no entra al repo.
"""
import argparse
import json
import os
import sys

os.environ.setdefault("WORKER_API_URL", "http://banco.invalid")
os.environ.setdefault("WORKER_SECRET", "banco")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import librosa  # noqa: E402
import numpy as np  # noqa: E402
import worker  # noqa: E402


def preparar(ruta: str, bpm_seed: float, cache: str):
    """Audio a SR del análisis, BPM fino y ancla, igual que analyze() con semilla."""
    if os.path.exists(cache):
        d = np.load(cache)
        return d["y"], float(d["bpm"]), float(d["fb"])
    y, _ = librosa.load(ruta, sr=worker.SR, mono=True, duration=worker.MAX_DURATION)
    y22, sr22 = librosa.load(ruta, sr=worker.SR_GRID, mono=True, duration=worker.MAX_DURATION)
    bpm_fino, _, _ = worker.refine_bpm(y22, sr22, float(bpm_seed))
    bpm = bpm_fino or float(bpm_seed)
    fb = float(worker.compute_anchor(ruta, bpm)["ancla_ms"])
    np.savez(cache, y=y.astype(np.float32), bpm=bpm, fb=fb)
    return y, bpm, fb


def cues_y_energia(y: np.ndarray, bpm: float, fb: float) -> dict:
    sr = worker.SR
    dur_ms = len(y) / sr * 1000.0
    cues = worker.detect_cues(y, sr, bpm, fb) or worker.cues_respaldo(y, sr, bpm, fb) or []
    cues = worker.cuantizar_cues(cues, bpm, fb, dur_ms) if cues else []
    bands = worker.compute_bands(y, sr, worker.BUCKETS)
    energia = worker.compute_energy(librosa.feature.rms(y=y)[0], bands)
    return {
        "energia": energia,
        "cues": [{"s": round(c["positionMs"] / 1000.0, 3), "energia": c.get("energy"),
                  "label": c.get("label")} for c in cues],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--banco", required=True)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--salida", required=True)
    a = ap.parse_args()
    os.makedirs(a.cache, exist_ok=True)
    with open(a.banco, encoding="utf-8") as f:
        banco = json.load(f)
    res = {}
    for t in banco:
        tid = t["track_id"]
        rutas = [os.path.join(a.audio, f"{tid}.{ext}") for ext in ("mp3", "flac", "wav", "aiff")]
        ruta = next((r for r in rutas if os.path.exists(r)), None)
        if ruta is None:
            print(f"falta el audio de {tid}", flush=True)
            continue
        y, bpm, fb = preparar(ruta, t["bpm"], os.path.join(a.cache, f"{tid}.npz"))
        res[tid] = cues_y_energia(y, bpm, fb)
        print(f"{t['orden']:>2} {tid[:8]} bpm {bpm:.3f} ancla {fb:.0f} ms · energía {res[tid]['energia']} "
              f"(MIK {t['mik']['energia']}) · {len(res[tid]['cues'])} cues", flush=True)
    with open(a.salida, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
