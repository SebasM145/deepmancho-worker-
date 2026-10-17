"""Pico de memoria por trabajo DENTRO del contenedor del análisis (mismo Dockerfile que Railway).
Corre N trabajos seguidos con temas sintéticos de 7 min (MP3 320k) y, por cada uno, mide el pico
de RSS de: analyze, medir_sonoridad, compute_anchor (lo que hace CM2) y la copia de escucha.
Uso (en el contenedor): python medir.py [trabajos=6]"""
import os, sys, threading, time, subprocess
import numpy as np
os.environ.setdefault("WORKER_API_URL", "http://x"); os.environ.setdefault("WORKER_SECRET", "x")
sys.path.insert(0, "/app")
import worker  # noqa: E402

def rss_mb():
    with open("/proc/self/status") as f:
        for l in f:
            if l.startswith("VmRSS:"):
                return int(l.split()[1]) // 1024
    return 0

pico = {"v": 0}
def mon():
    while True:
        pico["v"] = max(pico["v"], rss_mb()); time.sleep(0.01)
threading.Thread(target=mon, daemon=True).start()

def tema(i, dur=420, sr=44100):
    rng = np.random.default_rng(i)
    t = np.arange(dur * sr) / sr; beat = 60 / (122 + i)
    f = t % beat
    x = np.sin(2 * np.pi * 55 * f) * np.exp(-f * 12) * 0.7 + 0.15 * rng.standard_normal(len(t)) * np.exp(-(t % (beat / 2)) * 40)
    x += 0.2 * np.sin(2 * np.pi * (220 + 20 * i) * t)
    p = f"/tmp/t{i}.wav"
    import soundfile as sf
    sf.write(p, np.stack([x, x * 0.9], 1).astype(np.float32), sr, subtype="PCM_16")
    mp3 = f"/tmp/t{i}.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", p, "-b:a", "320k", mp3], check=True)
    os.remove(p)
    return mp3

n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
temas = [tema(i) for i in range(n)]
print(f"entorno: MALLOC_ARENA_MAX={os.environ.get('MALLOC_ARENA_MAX')} MALLOC_MMAP_THRESHOLD_={os.environ.get('MALLOC_MMAP_THRESHOLD_')} "
      f"OPENBLAS_NUM_THREADS={os.environ.get('OPENBLAS_NUM_THREADS')} cpus_visibles={os.cpu_count()}", flush=True)
for i, p in enumerate(temas):
    base = rss_mb(); pico["v"] = base; fila = []
    for nombre, f in (("analyze", lambda: worker.analyze(p, bpm_seed=None)),
                      ("sonoridad", lambda: worker.medir_sonoridad(p)),
                      ("cm2_ancla", lambda: worker.compute_anchor(p, 122.0 + i)),
                      ("copia", lambda: worker.make_rendition(p))):
        pico["v"] = rss_mb(); t0 = time.time(); f(); time.sleep(0.05)
        fila.append(f"{nombre} {pico['v']} MB ({time.time()-t0:.0f} s)")
    worker.liberar_memoria(); time.sleep(0.2)
    print(f"trabajo {i}: base {base} MB · " + " · ".join(fila) + f" · tras liberar {rss_mb()} MB", flush=True)
