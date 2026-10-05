"""#75 C-2 · RAM pico y tiempo de render_set con un set largo, como en una réplica.

Usa las duraciones y BPM reales de los primeros 28 temas de la resubida de Germán
(4-oct, 181 min de audio). El audio es sintético, pero del mismo largo y formato que
decodifica el worker (44,1 kHz estéreo float32), y se genera tema por tema, igual que
en producción (se baja y decodifica uno a la vez). Corre el render real: plan de
transiciones con tempo igualado (estirado con ffmpeg), máster a -14 LUFS y MP3 256k.
Solo se simula la red (descarga y subida). No escribe en la base ni corre en Railway.

Uso: python bancos/banco_set.py  (deja la tabla en GITHUB_STEP_SUMMARY si existe)
"""
import os
import resource
import sys
import time

os.environ.setdefault("WORKER_API_URL", "http://banco.invalid")
os.environ.setdefault("WORKER_SECRET", "banco")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np  # noqa: E402
import worker  # noqa: E402

DURACIONES = [482, 443, 424, 256, 427, 484, 319, 399, 320, 363, 631, 324, 353, 394, 296, 146,
              315, 436, 284, 426, 423, 444, 407, 450, 360, 420, 439, 388]
BPMS = [120, 120, 123, 125, 131, 121, 122, 123, 120, 122, 122, 123, 120, 122, 125, 118,
        125, 120, 123, 127, 123, 128, 128, 128, 128, 128, 129, 120]
SR = worker.SET_SR


def tema(i):
    """Bombo y hat del largo real del tema i, estéreo float32 (lo que da _decode_pcm)."""
    dur, bpm = DURACIONES[i], BPMS[i]
    t = np.arange(int(dur * SR), dtype=np.float32) / SR
    fase = np.mod(t, np.float32(60.0 / bpm))
    golpe = np.exp(-fase * 30, dtype=np.float32) * np.sin(2 * np.pi * 55 * fase, dtype=np.float32)
    x = (0.6 * golpe + 0.05 * np.sin(2 * np.pi * (220 + 20 * i) * t, dtype=np.float32)).astype(np.float32)
    return np.stack([x, x], axis=1)


def main():
    n = len(DURACIONES)
    tracks = [{"id": str(i), "title": f"tema {i}", "bpm": BPMS[i], "audio_url": f"http://banco/{i}"} for i in range(n)]
    trans = [{"desde": str(i), "hasta": str(i + 1), "tipo": "mezcla", "salida_seg": DURACIONES[i] - 40.0,
              "entrada_seg": 0.0, "duracion_seg": 16 * 240 / BPMS[i], "compases": 16,
              "rate": BPMS[i] / BPMS[i + 1], "release_seg": 8 * 240 / BPMS[i + 1], "asimetria": 0.6,
              "graves_swap": True, "graves_swap_en": 0.5, "tempo_ratio": 1, "razon": None} for i in range(n - 1)]

    worker._bajar_tema = lambda url, dest: open(dest, "w").write(url.rsplit("/", 1)[1])
    worker._decode_pcm = lambda p, sr=SR: tema(int(open(p).read()))

    class Subida:
        status_code = 200

        def raise_for_status(self):
            pass

    worker.requests.put = lambda *a, **k: Subida()
    base = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t0 = time.time()
    dur, tl = worker.render_set({"spec": {"transiciones": trans}}, tracks, "http://banco/subida", "banco.mp3")
    seg = time.time() - t0
    pico = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    escala = 1024 if sys.platform != "darwin" else 1  # Linux da KB; macOS, bytes
    gb = lambda v: v * escala / 1e9  # noqa: E731
    fila = (f"| {n} temas · {sum(DURACIONES) / 60:.0f} min de audio | {dur / 60:.1f} min | {seg:.0f} s | "
            f"**{gb(pico):.2f} GB** | {gb(base):.2f} GB |")
    tabla = ("### #75 C-2 · set largo en una réplica\n\n| entrada | set resultante | tiempo de render | RAM pico del proceso"
             " | RAM antes del render |\n|---|---|---|---|---|\n" + fila + "\n")
    print(tabla)
    destino = os.environ.get("GITHUB_STEP_SUMMARY")
    if destino:
        with open(destino, "a", encoding="utf-8") as f:
            f.write(tabla)


if __name__ == "__main__":
    main()
