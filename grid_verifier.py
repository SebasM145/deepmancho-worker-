#!/usr/bin/env python3
"""
DeepMancho — verificador de rejilla en servidor (v2, 3-sep-2026).

Hace que TODA pista del catálogo (existente o nueva) quede verificada contra
su propio audio sin intervención humana. Mismo repo y misma imagen que el
worker de análisis; en Railway corre como un segundo servicio con
`python -u grid_verifier.py`.

Bucle:
  POST /grid-verify-next  (x-worker-secret) → job + rendición firmada
  descarga → ffmpeg → PCM mono → envolvente de graves → pliegue de tempo
  y fase (PORT EXACTO de src/lib/mixer/gridVerify.ts + lowEnvelope.ts del
  mixer, commit 11f31dbf) → POST /grid-verify-result → RPC report_grid_audio
  (la ÚNICA puerta que decide si se aplica).

Variables de entorno (las mismas del worker + dos):
  WORKER_API_URL         https://<ref>.supabase.co/functions/v1
  WORKER_SECRET          mismo secreto que worker-next
  GRID_VERIFY_APPLY      "1" aplica; cualquier otra cosa = solo medir
  POLL_INTERVAL_SECONDS  espera con cola vacía (20)
  MAX_TRACK_MB           tope de descarga (60)

Paridad con el mixer: `python grid_verifier.py --bench archivo.mp3 bpm ancla_ms`
debe dar las mismas cifras que el bench del mixer (ver README).
"""
from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import numpy as np

VERSION = "1.0.0-py"
MIXER_SOURCE_COMMIT = "11f31dbf"

# ── Constantes (idénticas a gridVerify.ts) ───────────────────────────────────
GRID_VERIFY_GATE = {"minConf": 0.45, "minWindowsGood": 6, "minPeakRatio": 1.2, "maxResidualBeats": 0.08}
PHASE_GATE = {"minPeakRatio": 1.25, "minSharp": 1.5, "priorRadiusBeats": 0.25, "farPeakRatio": 2.5}
FOLD_BINS = 96
NOMINAL_HZ = 500
CUTOFF_HZ = 150


def jround(x: float) -> float:
    """Math.round de JS (mitades hacia +∞), no el redondeo bancario de Python."""
    return math.floor(x + 0.5)


def r3(x: float) -> float:
    return jround(x * 1000) / 1000


# ── lowEnvelope.ts ───────────────────────────────────────────────────────────
def low_envelope(pcm: np.ndarray, sr: int, nominal_hz: int = NOMINAL_HZ, cutoff_hz: float = CUTOFF_HZ):
    """Envolvente de graves |paso-bajo un polo| promediada por bin. Devuelve
    (env float32, bin_rate EXACTA = sr / samples_per_bin)."""
    spb = max(1, int(jround(sr / nominal_hz)))
    bin_rate = sr / spb
    rc = 1 / (2 * math.pi * cutoff_hz)
    dt = 1 / sr
    a = dt / (rc + dt)
    x = pcm.astype(np.float64, copy=False)
    # lp[i] = lp[i-1] + a*(x[i] - lp[i-1])  ⇔  lp[i] = a*x[i] + (1-a)*lp[i-1]
    try:
        from scipy.signal import lfilter  # type: ignore

        lp = lfilter([a], [1.0, -(1.0 - a)], x)
    except Exception:  # sin scipy: recursión pura (lenta pero exacta)
        lp = np.empty_like(x)
        acc = 0.0
        for i in range(x.shape[0]):
            acc = acc + a * (x[i] - acc)
            lp[i] = acc
    n_bins = x.shape[0] // spb
    env64 = np.abs(lp[: n_bins * spb]).reshape(n_bins, spb).mean(axis=1)
    return env64.astype(np.float32), bin_rate


# ── gridVerify.ts ────────────────────────────────────────────────────────────
def onset_raw(env: np.ndarray) -> np.ndarray:
    e = env.astype(np.float64)
    out = np.zeros(e.shape[0], dtype=np.float64)
    d = e[1:] - e[:-1]
    out[1:] = np.where(d > 0, d, 0.0)
    return out.astype(np.float32)


def onset_attack(env: np.ndarray, k: int = 3, floor_frac: float = 1e-3) -> np.ndarray:
    e = env.astype(np.float64)
    n = e.shape[0]
    out = np.zeros(n, dtype=np.float64)
    mx = float(e.max()) if n else 0.0
    floor = max(1e-12, floor_frac * mx)
    if n > k:
        d = np.log(e[k:] + floor) - np.log(e[:-k] + floor)
        out[k:] = np.where(d > 0, d, 0.0)
    return out.astype(np.float32)


def fold01(x: float) -> float:
    f = x - math.floor(x)
    if f >= 0.5:
        f -= 1
    return f


def fold_histogram(osf: np.ndarray, period_bins: float, bins: int = FOLD_BINS) -> np.ndarray:
    v = osf.astype(np.float64)
    inv = 1.0 / period_bins
    i = np.arange(v.shape[0], dtype=np.float64)
    f = i * inv
    b = np.floor((f - np.floor(f)) * bins).astype(np.int64)
    b[b >= bins] = bins - 1
    mask = v > 0
    return np.bincount(b[mask], weights=v[mask], minlength=bins).astype(np.float64)


def fold_peak(H: np.ndarray):
    bins = H.shape[0]
    S = (np.roll(H, 1) + 2 * H + np.roll(H, -1)) / 4.0
    mean = float(S.sum()) / bins
    peak = int(np.argmax(S))  # primer máximo, como el bucle de JS
    lobe = 3
    x = y = 0.0
    for d in range(-lobe, lobe + 1):
        b = (peak + d + bins) % bins
        ang = 2 * math.pi * (b + 0.5) / bins
        x += H[b] * math.cos(ang)
        y += H[b] * math.sin(ang)
    phase = (peak + 0.5) / bins if (x == 0 and y == 0) else math.atan2(y, x) / (2 * math.pi)
    phase -= math.floor(phase)
    second = 0.0
    for b in range(bins):
        dist = min(abs(b - peak), bins - abs(b - peak))
        if dist <= 8:
            continue
        if S[b] > second:
            second = float(S[b])
    return {
        "phase": phase,
        "sharp": float(S[peak]) / mean if mean > 0 else 0.0,
        "peakRatio": min(99.0, float(S[peak]) / second) if second > 0 else 99.0,
        "peakBin": peak,
        "smoothed": S,
    }


def tempo_sharpness(osf: np.ndarray, bin_rate: float, bpm: float) -> float:
    return fold_peak(fold_histogram(osf, (60 / bpm) * bin_rate))["sharp"]


def scan_tempo(osf, bin_rate, centers, span=1.0, step=0.02, fine_step=0.002):
    best = {"bpm": centers[0], "sharp": -1.0}
    seen = set()
    for c in centers:
        b = c - span
        while b <= c + span + 1e-9:
            key = int(jround(b * 1000))
            if key not in seen:
                seen.add(key)
                sh = tempo_sharpness(osf, bin_rate, b)
                if sh > best["sharp"]:
                    best = {"bpm": b, "sharp": sh}
            b += step
    coarse = best["bpm"]
    b = coarse - 3 * step
    while b <= coarse + 3 * step + 1e-9:
        sh = tempo_sharpness(osf, bin_rate, b)
        if sh > best["sharp"]:
            best = {"bpm": b, "sharp": sh}
        b += fine_step
    return {"bpm": r3(best["bpm"]), "sharp": best["sharp"]}


def _keep(prior, reason, sharp, pr):
    return {"anchorSec": prior, "deltaSec": 0.0, "sharp": sharp, "peakRatio": pr, "ok": False, "reason": reason}


def phase_at_tempo(osf_attack, bin_rate, bpm, prior_anchor_sec):
    period_sec = 60 / bpm
    period_bins = period_sec * bin_rate
    H = fold_histogram(osf_attack, period_bins)
    pk = fold_peak(H)
    if not (pk["sharp"] >= PHASE_GATE["minSharp"]):
        return _keep(prior_anchor_sec, "phase_flat", pk["sharp"], pk["peakRatio"])
    prior_phase = (prior_anchor_sec / period_sec) % 1
    d_global = fold01(pk["phase"] - prior_phase)
    if abs(d_global) <= PHASE_GATE["priorRadiusBeats"]:
        if not (pk["peakRatio"] >= PHASE_GATE["minPeakRatio"]):
            return _keep(prior_anchor_sec, "phase_ambiguous", pk["sharp"], pk["peakRatio"])
        return {"anchorSec": prior_anchor_sec + d_global * period_sec, "deltaSec": d_global * period_sec,
                "sharp": pk["sharp"], "peakRatio": pk["peakRatio"], "ok": True, "reason": "near_prior"}
    if pk["peakRatio"] >= PHASE_GATE["farPeakRatio"]:
        return {"anchorSec": prior_anchor_sec + d_global * period_sec, "deltaSec": d_global * period_sec,
                "sharp": pk["sharp"], "peakRatio": pk["peakRatio"], "ok": True, "reason": "far_strong"}
    bins = H.shape[0]
    S = pk["smoothed"]
    prior_bin = int(math.floor(prior_phase * bins)) % bins
    radius = int(jround(PHASE_GATE["priorRadiusBeats"] * bins))
    lb, lv = -1, -1.0
    for d in range(-radius, radius + 1):
        b = (prior_bin + d + bins) % bins
        if S[b] > lv:
            lv = float(S[b])
            lb = b
    mean = float(S.sum()) / bins
    local_sharp = lv / mean if mean > 0 else 0.0
    if lb >= 0 and local_sharp >= PHASE_GATE["minSharp"] and lv >= 0.6 * S[pk["peakBin"]]:
        x = y = 0.0
        for d in range(-3, 4):
            b = (lb + d + bins) % bins
            ang = 2 * math.pi * (b + 0.5) / bins
            x += H[b] * math.cos(ang)
            y += H[b] * math.sin(ang)
        ph = math.atan2(y, x) / (2 * math.pi)
        ph -= math.floor(ph)
        d_local = fold01(ph - prior_phase)
        top = float(S[pk["peakBin"]])
        return {"anchorSec": prior_anchor_sec + d_local * period_sec, "deltaSec": d_local * period_sec,
                "sharp": local_sharp, "peakRatio": (lv / top) if top > 0 else 1.0, "ok": True, "reason": "local_near_prior"}
    return _keep(prior_anchor_sec, "phase_far_weak", pk["sharp"], pk["peakRatio"])


def coverage(env, bin_rate, seg_sec=8):
    seg = max(1, int(jround(seg_sec * bin_rate)))
    e = env.astype(np.float64)
    n = e.shape[0] // seg
    if n == 0:
        return {"good": 0, "total": 0}
    sums = e[: n * seg].reshape(n, seg).sum(axis=1)
    mx = float(sums.max()) if n else 0.0
    return {"good": int((sums >= 0.15 * mx).sum()), "total": int(n)}


def assemble_result(env, bin_rate, prior_anchor_sec, catalog_bpm, tempo, phase):
    cov = coverage(env, bin_rate)
    conf = max(0.0, min(1.0, (tempo["sharp"] - 1) / 1.5))
    anchor_ms = max(0, int(jround(phase["anchorSec"] * 1000)))
    return {
        "anchorMs": anchor_ms,
        "bpm": r3(tempo["bpm"]),
        "deltaAnchorMs": jround(phase["deltaSec"] * 1000 * 10) / 10,
        "deltaBpm": r3(tempo["bpm"] - catalog_bpm),
        "conf": r3(conf),
        "peakRatio": (jround(phase["peakRatio"] * 100) / 100) if phase["ok"] else 9.9,
        "windowsGood": cov["good"],
        "windowsTotal": cov["total"],
        "residualBeats": 0,
        "tempoSharp": jround(tempo["sharp"] * 100) / 100,
        "phaseSharp": jround(phase["sharp"] * 100) / 100,
        "phaseOk": phase["ok"],
        "phaseReason": phase["reason"],
        "candidateBpm": catalog_bpm,
    }


def verify_grid_whole(env, bin_rate, prior_anchor_sec, candidates_bpm, span=1.0, step=0.02, fine_step=0.002):
    centers = []
    for v in candidates_bpm:
        v = r3(v)
        if not (40 <= v <= 220):
            continue
        if any(abs(w - v) < 0.5 for w in centers):
            continue
        centers.append(v)
    if not centers or env.shape[0] < bin_rate * 20:
        return None
    raw = onset_raw(env)
    attack = onset_attack(env)
    tempo = scan_tempo(raw, bin_rate, centers, span, step, fine_step)
    phase = phase_at_tempo(attack, bin_rate, tempo["bpm"], prior_anchor_sec)
    return assemble_result(env, bin_rate, prior_anchor_sec, centers[0], tempo, phase)


def passes_grid_gate(r) -> bool:
    if not r:
        return False
    g = GRID_VERIFY_GATE
    return (r["conf"] >= g["minConf"] and r["windowsGood"] >= g["minWindowsGood"]
            and r["peakRatio"] >= g["minPeakRatio"] and r["residualBeats"] <= g["maxResidualBeats"])


def bpm_fine_for(int_bpm: int, bpm: float) -> float:
    return r3(bpm - int_bpm)


# ── effBpm.ts / gridAnchor.ts ────────────────────────────────────────────────
BPM_CONFLICT_THRESHOLD = 0.5


def _fpos(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) and n > 0 else None


def stored_bpm(t):
    base = t.get("bpm")
    if base is None:
        return None
    base = float(base)
    fine = t.get("bpm_fine")
    fine = float(fine) if fine is not None else 0.0
    v = base + (fine if math.isfinite(fine) else 0.0)
    return v if math.isfinite(v) and v > 0 else None


def eff_bpm(t):
    stored = stored_bpm(t)
    det = _fpos(t.get("bpm_detected"))
    if stored is None:
        return det
    if det is not None and abs(det - stored) > BPM_CONFLICT_THRESHOLD:
        return det
    return stored


def first_beat_ms_of(t):
    if t.get("grid_source") in ("manual", "audio"):
        m = t.get("first_beat_offset_ms")
        if m is not None and math.isfinite(float(m)):
            return float(m)
    d = t.get("first_beat_detected_ms")
    if d is not None and math.isfinite(float(d)):
        return float(d)
    g = t.get("first_beat_offset_ms")
    if g is not None and math.isfinite(float(g)):
        return float(g)
    return None


# ── Decodificación ───────────────────────────────────────────────────────────
def probe_sample_rate(path: str) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=sample_rate",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, timeout=30)
    sr = int(out.stdout.strip() or 0)
    if out.returncode != 0 or sr <= 0:
        raise RuntimeError(f"ffprobe_failed:{out.stderr[:200]}")
    return sr


def decode_file(path: str):
    """PCM float32 mono, tasa nativa. Mono = (L+R)/2 como en el navegador."""
    sr = probe_sample_rate(path)
    p = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-vn", "-ac", "1", "-ar", str(sr),
         "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1"],
        capture_output=True, timeout=180)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg_failed:{p.stderr[:300].decode(errors='ignore')}")
    pcm = np.frombuffer(p.stdout, dtype=np.float32)
    return pcm, sr


# ── Medición de una pista ────────────────────────────────────────────────────
def measure_track(track: dict, pcm: np.ndarray, sr: int) -> dict:
    catalog_bpm = eff_bpm(track)
    prior_ms = first_beat_ms_of(track)
    if not catalog_bpm:
        raise RuntimeError("no_bpm")
    if prior_ms is None:
        raise RuntimeError("no_anchor")
    if pcm.shape[0] / sr < 30:
        raise RuntimeError("too_short")
    t0 = time.perf_counter()
    env, bin_rate = low_envelope(pcm, sr)
    t1 = time.perf_counter()
    centers = [catalog_bpm]
    for k in ("bpm_tag", "bpm_precise", "bpm_detected"):
        v = _fpos(track.get(k))
        if v:
            centers.append(v)
    pick = verify_grid_whole(env, bin_rate, prior_ms / 1000, centers)
    t2 = time.perf_counter()
    if not pick:
        raise RuntimeError("no_fit")
    int_bpm = int(track["bpm"]) if track.get("bpm") is not None else int(jround(pick["bpm"]))
    return {
        "anchorMs": pick["anchorMs"],
        "bpmFine": bpm_fine_for(int_bpm, pick["bpm"]),
        "conf": pick["conf"],
        "peakRatio": pick["peakRatio"],
        "windowsGood": pick["windowsGood"],
        "windowsTotal": pick["windowsTotal"],
        "residualBeats": pick["residualBeats"],
        "passesGate": passes_grid_gate(pick),
        "pick": pick,
        "catalogBpm": catalog_bpm,
        "priorAnchorMs": prior_ms,
        "costMs": {"env": int((t1 - t0) * 1000), "fit": int((t2 - t1) * 1000)},
        "binRate": bin_rate,
        "sampleRate": sr,
    }


# ── Cliente de las edge functions ────────────────────────────────────────────
API = os.environ.get("WORKER_API_URL", "").rstrip("/")
SECRET = os.environ.get("WORKER_SECRET", "")
APPLY = os.environ.get("GRID_VERIFY_APPLY", "0") == "1"
POLL_S = max(5, int(float(os.environ.get("POLL_INTERVAL_SECONDS", "20"))))
MAX_MB = max(5, int(float(os.environ.get("MAX_TRACK_MB", "60"))))


def log(msg: str, **extra):
    print(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "msg": msg, **extra}), flush=True)


def api(path: str, body: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        f"{API}/{path}", data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "x-worker-secret": SECRET}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{path} {e.code}: {e.read()[:300].decode(errors='ignore')}") from e


def download(url: str, dest: str) -> int:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=120) as r:
        ln = int(r.headers.get("content-length") or 0)
        if ln > MAX_MB * 1024 * 1024:
            raise RuntimeError(f"file_too_large:{ln}")
        data = r.read(MAX_MB * 1024 * 1024 + 1)
        if len(data) > MAX_MB * 1024 * 1024:
            raise RuntimeError(f"file_too_large:{len(data)}")
    with open(dest, "wb") as f:
        f.write(data)
    return len(data)


def measurement_payload(m: dict, path: str | None) -> dict:
    p = m["pick"]
    return {
        "anchor_ms": m["anchorMs"],
        "bpm_fine": m["bpmFine"],
        "conf": m["conf"],
        "peak_ratio": m["peakRatio"],
        "windows_good": m["windowsGood"],
        "windows_total": m["windowsTotal"],
        "residual_beats": m["residualBeats"],
        "client": {
            "v": 2,
            "verifier": f"grid_verifier.py@{VERSION}",
            "mixerSource": MIXER_SOURCE_COMMIT,
            "mode": "apply" if APPLY else "dry",
            "decoder": "ffmpeg",
            "path": path,
            "sampleRate": m["sampleRate"],
            "binRate": jround(m["binRate"] * 1000) / 1000,
            "candidateBpm": p["candidateBpm"],
            "measuredBpm": p["bpm"],
            "deltaAnchorMs": p["deltaAnchorMs"],
            "deltaBpm": p["deltaBpm"],
            "tempoSharp": p["tempoSharp"],
            "phaseSharp": p["phaseSharp"],
            "phaseOk": p["phaseOk"],
            "phaseReason": p["phaseReason"],
            "passesGate": m["passesGate"],
            "costMs": m["costMs"],
        },
    }


def process_one() -> bool:
    nxt = api("grid-verify-next", {})
    job = nxt.get("job")
    if not job:
        if nxt.get("skipped"):
            log("skipped", skipped=nxt["skipped"])
            return True
        return False
    track = nxt.get("track") or {}
    audio_url = nxt.get("audio_url")
    t0 = time.perf_counter()
    try:
        if not audio_url:
            raise RuntimeError("no_audio_url")
        path = track.get("path") or ""
        ext = "mp3" if path.lower().endswith(".mp3") else "m4a"
        with tempfile.TemporaryDirectory(prefix="gv-") as d:
            f = os.path.join(d, f"in.{ext}")
            nbytes = download(audio_url, f)
            pcm, sr = decode_file(f)
        m = measure_track(track, pcm, sr)
        res = api("grid-verify-result", {
            "job_id": job["id"], "track_id": job["track_id"], "ok": True, "apply": APPLY,
            "measurement": measurement_payload(m, track.get("path")),
        })
        log("verified", job=job["id"], track=job["track_id"], bytes=nbytes, sr=sr, bpm=m["pick"]["bpm"],
            dAnchor=m["pick"]["deltaAnchorMs"], dBpm=m["pick"]["deltaBpm"], conf=m["conf"], gate=m["passesGate"],
            rpc=res.get("result"), ms=int((time.perf_counter() - t0) * 1000))
    except Exception as e:  # noqa: BLE001
        err = str(e)[:500]
        log("failed", job=job["id"], track=job["track_id"], error=err, ms=int((time.perf_counter() - t0) * 1000))
        try:
            api("grid-verify-result", {"job_id": job["id"], "track_id": job["track_id"], "ok": False, "error": err})
        except Exception as e2:  # noqa: BLE001
            log("report_failed", job=job["id"], error=str(e2)[:300])
    return True


_stop = False


def _on_signal(*_):
    global _stop
    _stop = True


def main_loop():
    if not API or not SECRET:
        print("[grid_verifier] faltan WORKER_API_URL / WORKER_SECRET", file=sys.stderr)
        sys.exit(1)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    log("start", version=VERSION, apply=APPLY, pollS=POLL_S, api=API, mixerSource=MIXER_SOURCE_COMMIT)
    while not _stop:
        busy = False
        try:
            busy = process_one()
        except Exception as e:  # noqa: BLE001
            log("loop_error", error=str(e)[:300])
        if not busy:
            for _ in range(POLL_S):
                if _stop:
                    break
                time.sleep(1)
    log("stop")


def bench(argv):
    """python grid_verifier.py --bench archivo bpm ancla_ms [bpm_fine] [bpm_tag]"""
    path, bpm, anchor = argv[0], int(argv[1]), float(argv[2])
    fine = float(argv[3]) if len(argv) > 3 else 0.0
    tag = float(argv[4]) if len(argv) > 4 else None
    track = {"bpm": bpm, "bpm_fine": fine, "bpm_tag": tag, "bpm_precise": None, "bpm_detected": None,
             "first_beat_offset_ms": anchor, "first_beat_detected_ms": anchor, "grid_source": "detectada"}
    t0 = time.perf_counter()
    pcm, sr = decode_file(path)
    dec = int((time.perf_counter() - t0) * 1000)
    m = measure_track(track, pcm, sr)
    out = {k: v for k, v in m.items() if k != "pick"}
    out["pick"] = m["pick"]
    out["decodeMs"] = dec
    print(json.dumps(out))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--bench":
        bench(sys.argv[2:])
    else:
        main_loop()
