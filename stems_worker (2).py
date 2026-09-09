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
# La máquina tiene 24 núcleos y demucs usaba 2,4. JOBS procesa trozos en
# paralelo (cada uno pide memoria: 4 jobs ≈ 8 GB, entra de sobra en 24 GB).
JOBS = str(max(1, int(os.environ.get("DEMUCS_JOBS", "4"))))
# overlap 0.25 es el defecto; 0.15 acelera ~15 % con diferencia inaudible.
OVERLAP = str(float(os.environ.get("DEMUCS_OVERLAP", "0.15")))
os.environ.setdefault("OMP_NUM_THREADS", os.environ.get("TORCH_THREADS", "6"))
os.environ.setdefault("MKL_NUM_THREADS", os.environ.get("TORCH_THREADS", "6"))
MAX_MB = int(os.environ.get("MAX_TRACK_MB", "60"))
HEADERS = {"x-worker-secret": SECRET, "Content-Type": "application/json"}
VERSION = "1.15"
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


def run_demucs(src: Path, outdir: Path, model: str = MODEL) -> dict[str, Path]:
    cmd = [
        sys.executable, "-m", "demucs", "-n", model, "-d", "cpu",
        "--segment", SEGMENT, "-j", JOBS, "--overlap", OVERLAP, "--mp3", "--mp3-bitrate", "192", "-o", str(outdir), str(src),
    ]
    log("demucs:", " ".join(cmd[2:]))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-1500:]
        raise RuntimeError(f"demucs salió con {proc.returncode}: {tail}")
    # OJO: demucs guarda en <salida>/<modelo>/<nombre>/ — hay que usar el
    # modelo que se le pasó, no el de por defecto. (Bug de la v1.9.)
    base = outdir / model / src.stem
    stems = {p.stem: p for p in base.glob("*.mp3")}
    if not stems:
        encontrado = [str(p.relative_to(outdir)) for p in outdir.rglob("*.mp3")][:8]
        raise RuntimeError(
            f"demucs no produjo stems en {base.relative_to(outdir)}"
            + (f"; sí hay: {encontrado}" if encontrado else "")
        )
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


def refine_anchor(kick_times, bpm: float, anchor_s: float) -> float:
    """Ajusta el ancla al pulso real del bombo.

    La rejilla se arma desde `anchor_s`. Si esa ancla está mal (o no viene,
    como en los temas generados, donde llega NULL y se asume 0), TODO sale
    corrido: el riff empieza en el paso equivocado, el patrón reusado entra a
    destiempo y el vaivén se mide mal.

    Como el bombo de club cae en el pulso, se mide cuánto se desvía cada golpe
    respecto de la rejilla y se corre el ancla esa cantidad. Es estimación de
    fase de toda la vida, hecha sobre el instrumento más confiable.
    """
    if len(kick_times) < 8:
        return anchor_s
    beat = 60.0 / bpm
    # Desvío de cada golpe respecto del pulso más cercano, en fracción de pulso.
    fases = ((np.asarray(kick_times) - anchor_s) / beat) % 1.0
    # Media circular: los desvíos viven en un círculo (0.99 y 0.01 están juntos).
    ang = 2 * np.pi * fases
    media = np.arctan2(np.sin(ang).mean(), np.cos(ang).mean()) / (2 * np.pi)
    if media < 0:
        media += 1.0
    if media > 0.5:
        media -= 1.0            # corregir hacia atrás si está más cerca por ese lado
    corr = media * beat
    # Cuánto de acuerdo están los golpes entre sí (0 = dispersos, 1 = clavados).
    fuerza = float(np.hypot(np.sin(ang).mean(), np.cos(ang).mean()))
    if fuerza < 0.5 or abs(corr) < 0.005:
        return anchor_s          # sin consenso o ya está bien: no tocar
    log(f"ancla corregida {corr*1000:+.0f} ms (acuerdo {fuerza:.2f})")
    return anchor_s + corr


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

    # BOMBO LIMPIO: el bajo se cuela en la banda de graves. Un bombo real trae
    # un transitorio ("clic") en 2–6 kHz que el bajo no tiene. Se exige ese
    # clic y, además, un solo golpe por corchea (el más fuerte).
    if len(kick_t):
        click = S[(freqs >= 2000) & (freqs < 6000)].sum(axis=0)
        low = S[(freqs >= 30) & (freqs < 150)].sum(axis=0)
        fr = lambda t: min(len(low) - 1, int(t * sr / hop))
        click_med = np.median(click[click > 0]) if np.any(click > 0) else 0.0
        kept = []
        for t in kick_t:
            i = fr(t)
            has_click = click[i] > 1.5 * click_med
            if has_click:
                kept.append((t, low[i]))
        # un bombo por corchea: si dos caen en la misma corchea, queda el más fuerte
        eighth = 60.0 / bpm / 2.0
        by_slot = {}
        for t, energy in kept:
            slot = int(round(t / eighth))
            if slot not in by_slot or energy > by_slot[slot][1]:
                by_slot[slot] = (t, energy)
        kick_t = np.array(sorted(t for t, _ in by_slot.values()))

    # El ancla manda sobre TODO lo que viene después: corregirla acá, con el
    # bombo ya limpio, antes de armar la rejilla.
    anchor_ms = refine_anchor(kick_t, bpm, anchor_ms / 1000.0) * 1000.0

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
# ───────────────────────── PERFIL DE SONIDO (para clonar) ────────────────────
# Mide, sobre cada stem, lo que un productor ajusta para reconstruir el sonido
# con su propio instrumento: afinación, cola, brillo, clic, sidechain. NO copia
# audio: devuelve números. Con esos números el Estudio arma un preset y lo
# valida A/B contra el stem original.

def _env_db(y, sr, hop=256):
    frames = np.lib.stride_tricks.sliding_window_view(y, hop)[::hop]
    rms = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)
    return 20 * np.log10(rms + 1e-9)


def _decay_ms(seg, sr, drop_db=20.0):
    """Tiempo hasta que la envolvente cae `drop_db` por debajo del pico."""
    if len(seg) < 64:
        return 0.0
    env = _env_db(seg, sr)
    if not len(env):
        return 0.0
    peak_i = int(np.argmax(env))
    thr = env[peak_i] - drop_db
    below = np.where(env[peak_i:] < thr)[0]
    frames = (below[0] if len(below) else len(env) - peak_i)
    return round(frames * 256 / sr * 1000.0, 1)


def _highpass(seg, sr, fc):
    """Pasa-altos de un polo: para medir hats sin el cuerpo del bombo."""
    a = np.exp(-2 * np.pi * fc / sr)
    out = np.zeros_like(seg); prev_x = 0.0; prev_y = 0.0
    for i, x in enumerate(seg):
        prev_y = a * (prev_y + x - prev_x); prev_x = x; out[i] = prev_y
    return out


def _snap_to_peak(y, sr, t, window=0.03):
    """La rejilla da el tiempo teórico; el golpe real puede estar unos ms antes
    o después. Devuelve el instante del pico de energía en esa ventana."""
    a = int(max(0, (t - window) * sr)); b = int(min(len(y), (t + window) * sr))
    if b - a < 32:
        return t
    return a / sr + int(np.argmax(np.abs(y[a:b]))) / sr


def _centroid_hz(seg, sr):
    if len(seg) < 512:
        return 0.0
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    freqs = np.fft.rfftfreq(len(seg), 1 / sr)
    tot = spec.sum()
    return round(float((spec * freqs).sum() / tot), 1) if tot > 0 else 0.0


def _band_ratio(seg, sr, lo, hi):
    if len(seg) < 64:
        return 0.0
    n = max(2048, len(seg))  # relleno con ceros: los tramos cortos (clic) también se miden
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)), n=n)) ** 2
    freqs = np.fft.rfftfreq(n, 1 / sr)
    tot = spec.sum()
    return round(float(spec[(freqs >= lo) & (freqs < hi)].sum() / tot), 3) if tot > 0 else 0.0


def _cutoff_hz(seg, sr, pct=0.9):
    """Frecuencia bajo la cual está el `pct` de la energía (proxy del corte de filtro)."""
    if len(seg) < 1024:
        return 0.0
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) ** 2
    freqs = np.fft.rfftfreq(len(seg), 1 / sr)
    cum = np.cumsum(spec) / (spec.sum() + 1e-12)
    return round(float(freqs[int(np.searchsorted(cum, pct))]), 1)


def _dominant_hz(seg, sr, lo=30, hi=200):
    if len(seg) < 1024:
        return 0.0
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    freqs = np.fft.rfftfreq(len(seg), 1 / sr)
    m = (freqs >= lo) & (freqs < hi)
    return round(float(freqs[m][int(np.argmax(spec[m]))]), 1) if m.any() else 0.0


def _hits(y, sr, onsets_s, pre=0.0, post=0.4, max_n=40, snap=True):
    """Trozos de audio alrededor de cada golpe, alineados al ataque real."""
    out = []
    for t in onsets_s[:max_n]:
        tt = _snap_to_peak(y, sr, t) if snap else t
        a = int(max(0, (tt - pre) * sr)); b = int(min(len(y), (tt + post) * sr))
        if b - a > 256:
            out.append(y[a:b])
    return out


def sound_profile(stems: dict, grid: dict, bpm: float, anchor_ms: float) -> dict:
    """Un perfil por rol con los parámetros que hacen falta para clonarlo."""
    prof = {}
    try:
        beat = 60.0 / bpm
        anchor = anchor_ms / 1000.0
        if "drums" in stems:
            y, sr = to_wav_mono(stems["drums"], 22050)
            spb = grid.get("steps_per_bar", 16)
            if not (grid.get("kick") or grid.get("hat")):
                log("rejilla de batería vacía: no se puede medir el perfil de percusión")
            def times_of(row_name):
                ts = []
                rows = grid.get(row_name) or []
                for bi, row in enumerate(rows):
                    for s, v in enumerate(row):
                        if v:
                            ts.append(anchor + bi * 4 * beat + s * (4 * beat / spb))
                return ts
            kick_hits = _hits(y, sr, times_of("kick"), post=0.5)
            if kick_hits:
                prof["kick"] = {
                    "tune_hz": float(np.median([_dominant_hz(h, sr) for h in kick_hits])),
                    "decay_ms": float(np.median([_decay_ms(h, sr) for h in kick_hits])),
                    "click_ratio": float(np.median([_band_ratio(h[: int(0.012 * sr)], sr, 2000, 6000) for h in kick_hits])),
                    "sub_ratio": float(np.median([_band_ratio(h, sr, 30, 80) for h in kick_hits])),
                }
            hat_hits = _hits(y, sr, times_of("hat"), post=0.25)
            if hat_hits:
                # Los hats se miden SOBRE LA BANDA ALTA: en la pista de batería
                # completa manda el bombo y el brillo sale falseado hacia abajo.
                hp = [_highpass(h, sr, 3000) for h in hat_hits]
                prof["hats"] = {
                    "centroid_hz": float(np.median([_centroid_hz(h, sr) for h in hp])),
                    "decay_ms": float(np.median([_decay_ms(h, sr, 15.0) for h in hp])),
                }
            snare_hits = _hits(y, sr, times_of("snare"), post=0.3)
            if snare_hits:
                prof["clap"] = {
                    "centroid_hz": float(np.median([_centroid_hz(h, sr) for h in snare_hits])),
                    "decay_ms": float(np.median([_decay_ms(h, sr) for h in snare_hits])),
                }
        if "bass" in stems:
            yb, sr = to_wav_mono(stems["bass"], 22050)
            mid = yb[len(yb) // 3: 2 * len(yb) // 3]
            seg = mid[: sr * 8] if len(mid) > sr * 8 else mid
            bass = {
                "cutoff_hz": _cutoff_hz(seg, sr),
                "centroid_hz": _centroid_hz(seg, sr),
                "harmonic_ratio": _band_ratio(seg, sr, 200, 2000),
                "sub_ratio": _band_ratio(seg, sr, 30, 90),
            }
            # Sidechain: cuánto cae el bajo justo después de cada bombo.
            kicks = []
            if "drums" in stems and grid.get("kick"):
                spb = grid.get("steps_per_bar", 16)
                for bi, row in enumerate(grid["kick"]):
                    for s, v in enumerate(row):
                        if v:
                            kicks.append(anchor + bi * 4 * beat + s * (4 * beat / spb))
            if kicks:
                env = _env_db(yb, sr)
                fr = lambda t: min(len(env) - 1, int(t * sr / 256))
                # Nivel de referencia del bajo cuando SÍ está sonando.
                active = env[env > (np.percentile(env, 60) - 12)]
                floor_db = float(np.percentile(active, 20)) if len(active) else -60.0
                dips = []
                for t in kicks[:200]:
                    justo = env[fr(t + 0.02)]     # apenas pega el bombo: el bajo está agachado
                    luego = env[fr(t + 0.18)]     # ya se recuperó
                    # Solo cuenta si el bajo está tocando en ese compás.
                    if luego > floor_db and np.isfinite(justo) and np.isfinite(luego):
                        dips.append(justo - luego)
                if len(dips) >= 8:
                    bass["sidechain_db"] = round(float(np.median(dips)), 1)
                    bass["sidechain_muestras"] = len(dips)
            prof["bass"] = bass
        if "other" in stems:
            yo, sr = to_wav_mono(stems["other"], 22050)
            mid = yo[len(yo) // 3: 2 * len(yo) // 3]
            seg = mid[: sr * 8] if len(mid) > sr * 8 else mid
            env = _env_db(seg, sr)
            # Ataque medio: pads suben lento, stabs suben rápido.
            rises = []
            for i in range(1, len(env)):
                if env[i] - env[i - 1] > 6:
                    j = i
                    while j < len(env) - 1 and env[j + 1] > env[j]:
                        j += 1
                    rises.append((j - i + 1) * 256 / sr * 1000.0)
            prof["other"] = {
                "centroid_hz": _centroid_hz(seg, sr),
                "cutoff_hz": _cutoff_hz(seg, sr),
                "attack_ms": round(float(np.median(rises)), 1) if rises else 0.0,
                "character": "stab" if rises and np.median(rises) < 40 else "pad",
            }
        if "vocals" in stems:
            yv, sr = to_wav_mono(stems["vocals"], 22050)
            prof["vocals"] = {"centroid_hz": _centroid_hz(yv[len(yv)//3: len(yv)//3 + sr*8], sr)}
    except Exception as e:  # noqa: BLE001
        log("perfil de sonido incompleto:", repr(e))
    for k, v in prof.items():
        prof[k] = {kk: (round(float(vv), 3) if isinstance(vv, (int, float, np.floating)) else vv) for kk, vv in v.items()}
    return prof


# ─────────────────────── PAQUETES LISTOS PARA USAR ───────────────────────
# El DJ no quiere 1.156 notas: quiere BLOQUES. "El riff del drop, 2 compases,
# aparece 43 veces". Acá la canción se corta sola en pedazos usables, cada uno
# con lo que hace falta para arrastrarlo a un tema.

def _notes_in(notes, start_beat, end_beat):
    """Notas de un tramo, con el tiempo llevado a cero."""
    out = []
    for n in notes:
        if start_beat <= n["b"] < end_beat:
            m = dict(n)
            m["b"] = round(n["b"] - start_beat, 3)
            out.append(m)
    return out


def _huella(notas, rejilla=0.25):
    """Firma de un bloque: posiciones y alturas.

    Las posiciones se redondean a la SEMICORCHEA, no al centésimo de tiempo.
    Con dos decimales, una nota corrida un milisegundo hacía que el mismo riff
    contara como dos bloques distintos, y en música real eso pasa siempre.
    La fuerza no entra en la firma: el mismo riff tocado más fuerte es el mismo
    riff.
    """
    return "|".join(sorted(f'{round(n["b"] / rejilla) * rejilla:.2f}:{n["n"]}' for n in notas))


def _similitud(a: set, b: set) -> float:
    """Cuánto se parecen dos compases: notas en común sobre notas totales."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def bloques_de(notes, total_bars, largos=(1, 2, 4), minimo=3, tope=4,
               rejilla=0.25, parecido=0.7):
    """Los bloques que valen la pena de una parte, ordenados por repetición.

    IMPORTANTE: se agrupa por PARECIDO, no por coincidencia exacta.

    Medido sobre canciones reales: exigiendo repetición idéntica, el mejor
    bloque de bajo de un tech house cubría el 3 % de la canción. No era un
    problema de detección: un bajo real nunca se repite exactamente igual —
    cambia una nota fantasma, se corre un golpe, varía la fuerza. Con un
    umbral de parecido del 70 %, el riff se reconoce como lo que es.

    De cada grupo se guarda el compás MÁS REPRESENTATIVO (el que más se parece
    a todos los demás del grupo), no el primero: así el bloque que se lleva el
    DJ es la versión típica del riff, no una variación de entrada.
    """
    if not notes or total_bars <= 0:
        return []
    notes = [dict(n, b=round(round(n["b"] / rejilla) * rejilla, 3)) for n in notes]
    salida = []
    for bars in largos:
        candidatos = []
        for bar in range(0, total_bars - bars + 1, bars):
            trozo = _notes_in(notes, bar * 4, (bar + bars) * 4)
            if len(trozo) < minimo:
                continue
            firma = {(round(n["b"], 2), n["n"]) for n in trozo}
            candidatos.append((bar, trozo, firma))
        usados, grupos = set(), []
        for i, (bar_i, trozo_i, firma_i) in enumerate(candidatos):
            if i in usados:
                continue
            grupo = [(bar_i, trozo_i, firma_i)]
            usados.add(i)
            for j in range(i + 1, len(candidatos)):
                if j in usados:
                    continue
                if _similitud(firma_i, candidatos[j][2]) >= parecido:
                    grupo.append(candidatos[j])
                    usados.add(j)
            if len(grupo) >= 2:
                grupos.append(grupo)
        for grupo in grupos:
            # El más representativo: el que más se parece al resto del grupo.
            mejor, mejor_puntaje = grupo[0], -1.0
            for cand in grupo:
                puntaje = sum(_similitud(cand[2], otro[2]) for otro in grupo)
                if puntaje > mejor_puntaje:
                    mejor, mejor_puntaje = cand, puntaje
            bar, trozo, _ = mejor
            salida.append({
                "bars": bars,
                "desde_compas": bar + 1,
                "repite": len(grupo),
                "aparece_en": sorted(b + 1 for b, _, _ in grupo)[:12],
                "cubre": round(min(1.0, len(grupo) * bars / max(1, total_bars)), 3),
                "notas": trozo,
            })
    salida.sort(key=lambda x: (-x["cubre"], x["bars"]))
    return salida[:tope]


def bloque_bateria(grid, tope=3):
    """Lo mismo para la batería: no son notas, es una rejilla de 16 pasos."""
    filas = ("kick", "snare", "hat")
    if not any(grid.get(f) for f in filas):
        return []
    compases = max(len(grid.get(f) or []) for f in filas)
    grupos = {}
    for i in range(compases):
        patron = {}
        for f in filas:
            fila = grid.get(f) or []
            patron[f] = fila[i] if i < len(fila) else [0] * 16
        if not any(sum(v) for v in patron.values()):
            continue
        h = "|".join("".join(str(x) for x in patron[f]) for f in filas)
        grupos.setdefault(h, []).append((i, patron))
    salida = []
    for h, apar in grupos.items():
        if len(apar) < 2:
            continue
        i, patron = apar[0]
        salida.append({
            "bars": 1, "desde_compas": i + 1, "repite": len(apar),
            "aparece_en": [x + 1 for x, _ in apar[:12]],
            "cubre": round(min(1.0, len(apar) / max(1, compases)), 3),
            "rejilla": patron,
        })
    salida.sort(key=lambda x: -x["repite"])
    return salida[:tope]


def mapa_arreglo(stems, bpm, anchor_ms, duration):
    """En qué compás entra y sale cada instrumento: la receta de la canción."""
    beat = 60.0 / bpm
    compas = 4 * beat
    total = max(1, int((duration - anchor_ms / 1000.0) / compas))
    mapa = {}
    for nombre, path in stems.items():
        try:
            y, sr = to_wav_mono(path, 22050)
        except Exception as e:  # noqa: BLE001
            log("mapa: no se pudo leer", nombre, repr(e))
            continue
        niveles = []
        for i in range(total):
            a = int((anchor_ms / 1000.0 + i * compas) * sr)
            b = min(len(y), int(a + compas * sr))
            niveles.append(float(np.sqrt((y[a:b] ** 2).mean() + 1e-12)) if b > a else 0.0)
        pico = max(niveles) if niveles else 0.0
        if pico <= 1e-6:
            continue
        # Umbral RELATIVO a su propio pico: un pad bajo también cuenta como que suena.
        umbral = pico * 0.12
        tramos, ini = [], None
        for i, n in enumerate(niveles):
            on = n > umbral
            if on and ini is None:
                ini = i
            elif not on and ini is not None:
                if i - ini >= 4:          # menos de 4 compases no es una entrada
                    tramos.append({"desde": ini + 1, "hasta": i})
                ini = None
        if ini is not None:
            tramos.append({"desde": ini + 1, "hasta": len(niveles)})
        mapa[nombre] = {"tramos": tramos, "energia": [round(n / pico, 3) for n in niveles]}
    return {"compases": total, "instrumentos": mapa}


def process(job: dict):
    job_id = job["job_id"]
    # bpm_fine NO es el tempo: es una corrección de milésimas sobre bpm.
    # Usarlo como tempo absoluto daba 0.001 BPM y toda la rejilla salía vacía.
    bpm = float(job.get("bpm") or 0) + float(job.get("bpm_fine") or 0)
    if not (60.0 <= bpm <= 200.0):
        raise RuntimeError(
            f"tempo fuera de rango ({bpm:.3f} BPM): sin tempo no hay compases ni patrones. "
            "Revisar bpm/bpm_fine de la canción."
        )
    BPM_GLOBAL[0] = bpm
    anchor_ms = float(job.get("first_beat_offset_ms") or 0)
    work = Path(tempfile.mkdtemp(prefix="stems-"))
    try:
        src = download(job["audio_url"], work / "input.mp3")
        duration = float(job.get("duration_seconds") or ffprobe_duration(src))
        log(f"[{job_id[:8]}] {duration:.0f}s @ {bpm:.2f} bpm, ancla {anchor_ms:.0f} ms")

        # El trabajo puede pedir otro modelo: htdemucs_6s separa piano y
        # guitarra en pistas propias (a cambio de algo menos de limpieza).
        model = str(job.get("model") or MODEL)
        if model not in ("htdemucs", "htdemucs_ft", "htdemucs_6s", "mdx_extra"):
            model = MODEL
        stems = run_demucs(src, work / "out", model)
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

        # PARTES COMPLETAS POR INSTRUMENTO
        # No alcanza con "el riff": el DJ quiere todo lo que toca cada
        # instrumento en la canción entera, para editarlo y reusarlo. Basic
        # Pitch es polifónico, así que un piano o unos pads salen con sus
        # acordes reales, no con una etiqueta por compás.
        parts = {}
        RANGES = {          # rango de notas MIDI razonable por instrumento
            "piano": (36, 96), "guitar": (40, 88), "other": (36, 96),
            "vocals": (48, 84), "bass": (24, 60),
        }
        for name, (lo, hi) in RANGES.items():
            if name not in stems:
                continue
            if name == "bass":
                parts["bass"] = bass_midi          # ya transcrito
                continue
            try:
                seq = midi_notes(stems[name], to_beat, lo, hi, max_notes=6000)
                if seq:
                    parts[name] = seq
                    log(f"[{job_id[:8]}] parte {name}: {len(seq)} notas")
            except Exception as e:  # noqa: BLE001
                log(f"[{job_id[:8]}] no se pudo transcribir {name}:", repr(e))
        grid = drum_grid(stems["drums"], bpm, anchor_ms, duration) if "drums" in stems else \
            {"steps_per_bar": 16, "bars": 0, "kick": [], "snare": [], "hat": []}
        chords = chords_per_bar([stems[s] for s in ("bass", "other", "piano") if s in stems], bpm, anchor_ms, grid["bars"])
        stats = stem_stats(stems, bass_midi, grid)

        profile = sound_profile(stems, grid, bpm, anchor_ms)
        # PAQUETES: lo que el DJ realmente usa, ya cortado y ordenado.
        total_bars = int(grid.get("bars") or 0) or 1
        blocks = {}
        try:
            for nombre, seq in parts.items():
                b = bloques_de(seq, total_bars)
                if b:
                    blocks[nombre] = b
            bat = bloque_bateria(grid)
            if bat:
                blocks["drums"] = bat
        except Exception as e:  # noqa: BLE001
            log(f"[{job_id[:8]}] no se pudieron armar los bloques:", repr(e))
        log(f"[{job_id[:8]}] bloques: " + (", ".join(f"{k} {len(v)}" for k, v in blocks.items()) or "ninguno"))

        # La variable se llama `duration` (v1.12 usaba `dur` y rompía el análisis).
        # Y si el mapa falla, NO se pierde todo el trabajo: se reporta sin él.
        try:
            arreglo = mapa_arreglo(stems, bpm, anchor_ms, duration)
            log(f"[{job_id[:8]}] arreglo: {len(arreglo['instrumentos'])} instrumentos en {arreglo['compases']} compases")
        except Exception as e:  # noqa: BLE001
            log(f"[{job_id[:8]}] no se pudo armar el mapa de arreglo:", repr(e))
            arreglo = None

        patterns = {"bass_midi": bass_midi, "melody_midi": melody_midi, "drum_grid": grid,
                    "chords": chords, "stats": stats, "model": model, "sound_profile": profile,
                    "parts": parts, "blocks": blocks, "arrangement_map": arreglo}
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
    log(f"stems_worker v{VERSION} listo · modelo {MODEL} · segmento {SEGMENT}s · jobs {JOBS} · overlap {OVERLAP} · hilos {os.environ.get('OMP_NUM_THREADS')} · sondeo cada {POLL}s")
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
