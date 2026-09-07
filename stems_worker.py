#!/usr/bin/env python3
"""
stems_worker.py — DJConnect · separación en pistas y extracción de patrones.

Mismo patrón que grid_verifier.py: sondea una Edge Function, procesa una
canción, sube resultados, reporta. No toca worker.py ni sus tablas.

Qué hace por canción:
  1. Descarga el audio (URL firmada que entrega stems-next).
  2. Demucs (htdemucs_6s por defecto): vocals, drums, bass, other, piano, guitar.
  3. Sube cada stem como MP3 a la URL firmada de subida (bucket privado).
  4. Extrae PATRONES (esto es lo que el DJ guarda y reusa; no es audio ajeno):
       - bass_midi / melody_midi : notas MIDI (Basic Pitch) en beats desde el ancla
       - drum_grid               : rejilla de 16 pasos por compás (kick / snare / hat)
       - chords                  : acorde por compás (plantillas mayor/menor sobre chroma)
       - stats                   : energía por stem, densidad, rango del bajo
  5. POST a stems-result.

Variables de entorno (Railway):
  WORKER_API_URL        p.ej. https://<proj>.supabase.co/functions/v1
  WORKER_SECRET         el mismo que usa el worker (header x-worker-secret)
  POLL_INTERVAL_SECONDS default 15
  STEMS_MODEL           default htdemucs_6s (alternativa más liviana: htdemucs)
  DEMUCS_SEGMENT        default 8 (segundos; menos = menos RAM, más lento)
  MAX_TRACK_MB          default 60
"""
from __future__ import annotations

import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

import numpy as np
import requests

API = os.environ["WORKER_API_URL"].rstrip("/")
SECRET = os.environ["WORKER_SECRET"]
POLL = int(os.environ.get("POLL_INTERVAL_SECONDS", "15"))
MODEL = os.environ.get("STEMS_MODEL", "htdemucs_6s")
# Los modelos htdemucs no aceptan segmentos > 7.8 s (largo de entrenamiento).
SEGMENT = str(int(min(7, int(float(os.environ.get("DEMUCS_SEGMENT", "7"))))))  # entero: demucs no acepta decimales
MAX_MB = int(os.environ.get("MAX_TRACK_MB", "60"))
HEADERS = {"x-worker-secret": SECRET, "Content-Type": "application/json"}
VERSION = "1.3"
STEP_DIV = 4  # 16 pasos por compás de 4/4


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ----------------------------------------------------------------------------- API
def claim():
    r = requests.post(f"{API}/stems-next", headers=HEADERS, json={}, timeout=60)
    if r.status_code == 204:
        return None
    r.raise_for_status()
    return r.json()


def report(job_id: str, ok: bool, stems=None, patterns=None, error: str | None = None):
    body = {"job_id": job_id, "ok": ok, "stems": stems or {}, "patterns": patterns or {}, "error": error}
    r = requests.post(f"{API}/stems-result", headers=HEADERS, json=body, timeout=120)
    if not r.ok:
        log("stems-result respondió", r.status_code, r.text[:300])
    r.raise_for_status()


def download(url: str, dest: Path):
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        size = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                size += len(chunk)
                if size > MAX_MB * (1 << 20):
                    raise RuntimeError(f"archivo mayor a {MAX_MB} MB")
                f.write(chunk)
    return dest


def upload(target, path: Path, content_type="audio/mpeg"):
    """`target` es el objeto {path, url, token} que entrega stems-next (o una URL simple)."""
    url = target["url"] if isinstance(target, dict) else target
    headers = {"Content-Type": content_type, "x-upsert": "true"}
    if isinstance(target, dict) and target.get("token") and "token=" not in url:
        headers["Authorization"] = f"Bearer {target['token']}"
    with open(path, "rb") as f:
        r = requests.put(url, data=f, headers=headers, timeout=600)
    if not r.ok:
        raise RuntimeError(f"subida falló {r.status_code}: {r.text[:200]}")


# ----------------------------------------------------------------------------- audio
def ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out or 0)


def to_wav_mono(path: Path, sr: int = 22050) -> tuple[np.ndarray, int]:
    """Decodifica con ffmpeg a float32 mono (sin depender de librosa.load/audioread)."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.float32), sr


def run_demucs(src: Path, outdir: Path) -> dict[str, Path]:
    cmd = [
        sys.executable, "-m", "demucs", "-n", MODEL, "-d", "cpu",
        "--segment", SEGMENT, "--mp3", "--mp3-bitrate", "192", "-o", str(outdir), str(src),
    ]
    log("demucs:", " ".join(cmd[2:]))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-1500:]
        raise RuntimeError(f"demucs salió con {proc.returncode}: {tail}")
    base = outdir / MODEL / src.stem
    stems = {p.stem: p for p in base.glob("*.mp3")}
    if not stems:
        raise RuntimeError("demucs no produjo stems")
    return stems


def lufs_of(path: Path) -> float | None:
    """Loudness integrada con el filtro de ffmpeg (sin pyloudnorm)."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-v", "info", "-i", str(path), "-af", "ebur128=peak=none", "-f", "null", "-"],
            capture_output=True, text=True,
        ).stderr
        for line in reversed(out.splitlines()):
            if "I:" in line and "LUFS" in line:
                return float(line.split("I:")[1].split("LUFS")[0].strip())
    except Exception:
        pass
    return None


# ----------------------------------------------------------------------------- patrones
def beat_grid(bpm: float, anchor_ms: float):
    ms_per_beat = 60000.0 / bpm
    def to_beat(t_sec: float) -> float:
        return (t_sec * 1000.0 - anchor_ms) / ms_per_beat
    return ms_per_beat, to_beat


def midi_notes(path: Path, to_beat, lo: int, hi: int, max_notes=4000):
    """Basic Pitch → notas en beats desde el ancla, cuantizadas a 1/4 de beat."""
    from basic_pitch.inference import predict
    from basic_pitch import ICASSP_2022_MODEL_PATH
    _, _, events = predict(str(path), ICASSP_2022_MODEL_PATH,
                           onset_threshold=0.5, frame_threshold=0.3, minimum_note_length=80)
    notes = []
    for start, end, pitch, amp, _bends in events:
        if not (lo <= pitch <= hi):
            continue
        b = round(to_beat(start) * STEP_DIV) / STEP_DIV
        d = max(0.25, round((end - start) / (60.0 / BPM_GLOBAL[0]) * STEP_DIV) / STEP_DIV)
        if b < 0:
            continue
        notes.append({"b": round(b, 3), "d": round(d, 3), "n": int(pitch), "v": round(float(amp), 3)})
    notes.sort(key=lambda x: (x["b"], x["n"]))
    return notes[:max_notes]


BPM_GLOBAL = [126.0]  # se fija por job; evita pasar bpm por todos lados


def drum_grid(path: Path, bpm: float, anchor_ms: float, duration: float):
    """Rejilla kick/snare/hat por semicorchea a partir de picos de energía por banda."""
    import librosa
    y, sr = to_wav_mono(path, 22050)
    if y.size < sr:
        return {"steps_per_bar": 16, "bars": 0, "kick": [], "snare": [], "hat": []}
    hop = 256
    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=hop)) ** 2
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)

    def band_onsets(f_lo, f_hi, delta):
        band = S[(freqs >= f_lo) & (freqs < f_hi)].sum(axis=0)
        env = librosa.onset.onset_strength(S=librosa.power_to_db(band[None, :] + 1e-9), sr=sr, hop_length=hop)
        peaks = librosa.util.peak_pick(env, pre_max=3, post_max=3, pre_avg=10, post_avg=10, delta=delta, wait=2)
        return librosa.frames_to_time(peaks, sr=sr, hop_length=hop)

    kick_t = band_onsets(30, 150, 0.6)
    snare_t = band_onsets(150, 2500, 0.5)
    hat_t = band_onsets(5000, 11000, 0.4)

    # La banda de medios recoge el cuerpo del bombo: un "golpe de caja" que
    # coincide (±25 ms) con un bombo y no tiene más energía en 1.5–4 kHz que
    # el bombo en su banda, es bombo colado. Se descarta.
    if len(kick_t) and len(snare_t):
        hi = S[(freqs >= 1500) & (freqs < 4000)].sum(axis=0)
        lo = S[(freqs >= 30) & (freqs < 150)].sum(axis=0)
        fr = lambda t: min(len(hi) - 1, int(t * sr / hop))
        keep = []
        for t in snare_t:
            near = np.abs(kick_t - t).min() < 0.025
            if near and hi[fr(t)] < 0.35 * lo[fr(t)]:
                continue
            keep.append(t)
        snare_t = np.array(keep)

    ms_per_beat, to_beat = beat_grid(bpm, anchor_ms)
    steps_per_bar = 16
    total_beats = max(0.0, to_beat(duration))
    bars = int(math.ceil(total_beats / 4))
    bars = min(bars, 512)

    def grid_for(times):
        g = [[0] * steps_per_bar for _ in range(bars)]
        for t in times:
            step = int(round(to_beat(float(t)) * STEP_DIV))
            if step < 0:
                continue
            bar, pos = divmod(step, steps_per_bar)
            if bar < bars:
                g[bar][pos] = 1
        return g

    return {"steps_per_bar": steps_per_bar, "bars": bars,
            "kick": grid_for(kick_t), "snare": grid_for(snare_t), "hat": grid_for(hat_t)}


_TEMPLATES = None
def chord_templates():
    global _TEMPLATES
    if _TEMPLATES is None:
        names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
        t = []
        for root in range(12):
            maj = np.zeros(12); maj[[root, (root + 4) % 12, (root + 7) % 12]] = 1
            mi = np.zeros(12); mi[[root, (root + 3) % 12, (root + 7) % 12]] = 1
            t.append((names[root], maj)); t.append((names[root] + "m", mi))
        _TEMPLATES = t
    return _TEMPLATES


def chords_per_bar(paths: list[Path], bpm: float, anchor_ms: float, bars: int):
    import librosa
    ys = []
    for p in paths:
        y, sr = to_wav_mono(p, 22050)
        ys.append(y)
    if not ys or bars == 0:
        return []
    n = min(len(y) for y in ys)
    y = np.sum([y[:n] for y in ys], axis=0) / len(ys)
    sr = 22050
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=512)
    ms_per_beat = 60000.0 / bpm
    out = []
    for bar in range(bars):
        t0 = (anchor_ms + bar * 4 * ms_per_beat) / 1000.0
        t1 = t0 + 4 * ms_per_beat / 1000.0
        f0, f1 = librosa.time_to_frames([t0, t1], sr=sr, hop_length=512)
        if f0 < 0 or f1 <= f0 or f0 >= chroma.shape[1]:
            continue
        v = chroma[:, f0:min(f1, chroma.shape[1])].mean(axis=1)
        if v.sum() <= 0:
            continue
        v = v / (np.linalg.norm(v) + 1e-9)
        best, score = None, -1.0
        for name, tmpl in chord_templates():
            s = float(v @ (tmpl / np.linalg.norm(tmpl)))
            if s > score:
                best, score = name, s
        out.append({"bar": bar, "chord": best, "conf": round(score, 3)})
    return out


def stem_stats(stems: dict[str, Path], bass_notes, grid):
    st = {}
    for name, p in stems.items():
        y, _ = to_wav_mono(p, 11025)
        st[f"rms_{name}"] = round(float(np.sqrt(np.mean(y ** 2)) if y.size else 0.0), 5)
    if bass_notes:
        ns = [n["n"] for n in bass_notes]
        st["bass_range"] = [min(ns), max(ns)]
        st["bass_notes"] = len(bass_notes)
    if grid.get("bars"):
        for k in ("kick", "snare", "hat"):
            st[f"{k}_density"] = round(sum(map(sum, grid[k])) / (grid["bars"] * 16), 3)
    return st


# ----------------------------------------------------------------------------- job
def process(job: dict):
    job_id = job["job_id"]
    bpm = float(job.get("bpm_fine") or job.get("bpm") or 126)
    BPM_GLOBAL[0] = bpm
    anchor_ms = float(job.get("first_beat_offset_ms") or 0)
    work = Path(tempfile.mkdtemp(prefix="stems-"))
    try:
        src = download(job["audio_url"], work / "input.mp3")
        duration = float(job.get("duration_seconds") or ffprobe_duration(src))
        log(f"[{job_id[:8]}] {duration:.0f}s @ {bpm:.2f} bpm, ancla {anchor_ms:.0f} ms")

        stems = run_demucs(src, work / "out")
        uploads = job.get("uploads", {})
        stems_out = {}
        for name, p in stems.items():
            if name in uploads:
                target = uploads[name]
                upload(target, p)
                stored_path = target["path"] if isinstance(target, dict) else None
                stems_out[name] = {"path": stored_path, "duration_seconds": round(ffprobe_duration(p), 2), "lufs": lufs_of(p)}
                log(f"  subido {name} -> {stored_path}")

        _, to_beat = beat_grid(bpm, anchor_ms)
        bass_midi = midi_notes(stems["bass"], to_beat, 24, 60) if "bass" in stems else []
        melody_src = [s for s in ("piano", "other", "guitar") if s in stems]
        melody_midi = midi_notes(stems[melody_src[0]], to_beat, 48, 96) if melody_src else []
        grid = drum_grid(stems["drums"], bpm, anchor_ms, duration) if "drums" in stems else \
            {"steps_per_bar": 16, "bars": 0, "kick": [], "snare": [], "hat": []}
        chords = chords_per_bar([stems[s] for s in ("bass", "other", "piano") if s in stems], bpm, anchor_ms, grid["bars"])
        stats = stem_stats(stems, bass_midi, grid)

        patterns = {"bass_midi": bass_midi, "melody_midi": melody_midi, "drum_grid": grid,
                    "chords": chords, "stats": stats, "model": MODEL}
        report(job_id, True, stems_out, patterns)
        log(f"[{job_id[:8]}] listo: {len(stems_out)} stems, {len(bass_midi)} notas de bajo, {grid['bars']} compases")
    except Exception as e:  # noqa: BLE001
        log(f"[{job_id[:8]}] FALLÓ:", repr(e))
        traceback.print_exc()
        try:
            report(job_id, False, error=str(e)[:500])
        except Exception:
            traceback.print_exc()
    finally:
        shutil.rmtree(work, ignore_errors=True)


CURRENT_JOB = {"id": None}


def requeue(job_id: str):
    """Devuelve a la cola un trabajo que este proceso no va a terminar."""
    try:
        requests.post(f"{API}/stems-result", headers=HEADERS,
                      json={"job_id": job_id, "ok": False, "error": "requeue:shutdown"}, timeout=15)
        log(f"[{job_id[:8]}] devuelto a la cola por apagado")
    except Exception as e:  # noqa: BLE001
        log("no se pudo devolver el trabajo:", repr(e))


def _on_shutdown(signum, _frame):
    log(f"señal {signum}: apagando")
    if CURRENT_JOB["id"]:
        requeue(CURRENT_JOB["id"])
    sys.exit(0)


def main():
    signal.signal(signal.SIGTERM, _on_shutdown)
    signal.signal(signal.SIGINT, _on_shutdown)
    log(f"stems_worker v{VERSION} listo · modelo {MODEL} · segmento {SEGMENT}s · sondeo cada {POLL}s")
    while True:
        try:
            job = claim()
            if job:
                CURRENT_JOB["id"] = job["job_id"]
                try:
                    process(job)
                finally:
                    CURRENT_JOB["id"] = None
                continue
        except Exception as e:  # noqa: BLE001
            log("error en el sondeo:", repr(e))
        time.sleep(POLL)


if __name__ == "__main__":
    main()
