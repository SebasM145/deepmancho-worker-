"""Banco del cue A (dj-connect #930) contra el primer sonido y el cue A de Mixed In Key.

Por cada tema del XML de Rekordbox con hot cue A (Num=0) y audio local mide: el A de 7.6.24
(primer downbeat de nuestra rejilla), el de 7.6.25 (`worker.posicion_cue_a_ms`), el primer
sonido (2 % del pico, en las muestras y en los `waveform_peaks` que mira el mezclador) y el A
de MIK. Solo lee: no escribe en la base ni sube nada. El audio y el XML son de German y no
entran al repo.

Uso:
  python bancos/banco_cue_a.py <rekordbox.xml> <carpeta cache> <salida.jsonl> [procesos]
  python bancos/banco_cue_a.py --medir <salida.jsonl>

Retoma donde quedo: salta los temas que ya estan en la salida. La parte cara (audio, BPM fino
y ancla) va a la cache, igual que en banco_mik.py. Cada proceso usa su propia cache de numba
(varios compilando a la vez en la misma la corrompen y librosa revienta con segfault), y si un
proceso muere el banco lo dice y rearma los procesos en vez de quedarse colgado.
"""
import json
import math
import os
import statistics as st
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool

os.environ.setdefault("WORKER_API_URL", "http://banco.invalid")
os.environ.setdefault("WORKER_SECRET", "banco")
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
sys.path.insert(0, os.path.join(RAIZ, "bancos"))

TOL_MS = 50  # tolerancia contra MIK: el MP3 se decodifica ~39 ms distinto en Rekordbox


def temas(xml):
    """Temas del XML con cue A de MIK, rejilla de Rekordbox y audio en este equipo."""
    out = []
    for t in ET.parse(xml).getroot().iter("TRACK"):
        loc = t.get("Location")
        tempo = t.find("TEMPO")
        a = [float(p.get("Start")) for p in t.findall("POSITION_MARK") if p.get("Num") == "0"]
        if not loc or tempo is None or not a:
            continue
        ruta = urllib.parse.unquote(loc.replace("file://localhost", ""))
        if os.path.exists(ruta):
            out.append(dict(id=t.get("TrackID"), ruta=ruta, bpm_rb=float(tempo.get("Bpm")),
                            inizio_ms=float(tempo.get("Inizio")) * 1000, mik_a_ms=a[0] * 1000))
    return out


def primer_downbeat_7624(fb, bpm):
    """El A de 7.6.24 (`primer_downbeat_ms`), para medir el antes."""
    if not (40 < bpm < 240) or fb <= 0:
        return 0
    bar = 4 * 60000.0 / bpm
    return max(0.0, fb - math.floor(fb / bar + 1e-6) * bar)


def medir_tema(args):
    t, cache = args
    try:
        import worker
        from banco_mik import preparar
        y, bpm, fb = preparar(t["ruta"], t["bpm_rb"], os.path.join(cache, t["id"] + ".npz"))
        dur = len(y) / worker.SR * 1000.0
        ini = worker.inicio_audible_ms(y, dur)
        return dict(t, bpm=bpm, fb=fb, dur_ms=dur, ini_muestras=ini,
                    ini_peaks=worker.inicio_audible_ms(worker.bucket_reduce(y, worker.BUCKETS, "peak"), dur),
                    a7624=primer_downbeat_7624(fb, bpm), a7625=worker.posicion_cue_a_ms(fb, bpm, ini))
    except Exception as e:  # noqa: BLE001 - un tema roto no para el banco
        return dict(t, error=repr(e)[:200])


def _cache_numba(base):
    """Cada proceso con su cache de numba, antes de importar librosa."""
    os.environ["NUMBA_CACHE_DIR"] = os.path.join(base, "numba", str(os.getpid()))


def correr(xml, cache, salida, procesos=4, intentos=5):
    os.makedirs(cache, exist_ok=True)
    for _ in range(intentos):
        hechos = set()
        if os.path.exists(salida):
            hechos = {json.loads(x)["id"] for x in open(salida)}
        pend = [t for t in temas(xml) if t["id"] not in hechos]
        print(f"pendientes {len(pend)}", flush=True)
        if not pend:
            return
        try:
            with ProcessPoolExecutor(procesos, initializer=_cache_numba, initargs=(cache,)) as p, \
                    open(salida, "a") as f:
                for fut in as_completed([p.submit(medir_tema, (t, cache)) for t in pend]):
                    f.write(json.dumps(fut.result()) + "\n")
                    f.flush()
            return
        except BrokenProcessPool as e:  # un proceso murio (segfault, memoria): se rearma y sigue
            print(f"proceso caido ({e!r}); rearmo y sigo", flush=True)


def _fase(pos, ancla, paso):
    d = (pos - ancla) % paso
    return min(d, paso - d)


def medir(salida):
    """Las dos preguntas del mezclador en #930 y el antes/despues contra MIK."""
    filas = [json.loads(x) for x in open(salida)]
    ok = [f for f in filas if "error" not in f and f.get("bpm") and f.get("ini_peaks") is not None]
    n = len(ok)

    def pct(k, d=n):
        return f"{k} ({100 * k / d:.0f} %)" if d else "0"

    print(f"temas {len(filas)} · con error {len(filas) - len(ok) if filas else 0} · medidos {n}")
    pisa = [f for f in ok if f["ini_peaks"] < f["a7624"] - 60000.0 / f["bpm"]]
    print(f"P1 · suena mas de un beat antes del A de 7.6.24 (el mezclador lo corrige): {pct(len(pisa))}")
    en_db = sum(_fase(f["mik_a_ms"], f["fb"], 240000.0 / f["bpm"]) <= TOL_MS for f in pisa)
    en_b = sum(_fase(f["mik_a_ms"], f["fb"], 60000.0 / f["bpm"]) <= TOL_MS for f in pisa)
    en_rb = sum(abs(f["mik_a_ms"] - f["inizio_ms"]) <= 5 for f in pisa)
    print(f"P2 · ahi el A de MIK esta en el primer beat de Rekordbox {pct(en_rb, len(pisa))}, en un beat "
          f"nuestro {pct(en_b, len(pisa))}, en nuestro downbeat {pct(en_db, len(pisa))}")
    for v in ("a7624", "a7625"):
        d = [abs(f[v] - f["mik_a_ms"]) for f in ok]
        resp = sum(f[v] <= max(f["ini_peaks"], 0) + 60000.0 / f["bpm"] for f in ok)
        print(f"{v}: |A - MIK| mediana {st.median(d):.0f} ms · <= {TOL_MS} ms {pct(sum(x <= TOL_MS for x in d))}"
              f" · el mezclador lo respeta {pct(resp)}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--medir"]:
        medir(sys.argv[2])
    else:
        correr(sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]) if len(sys.argv) > 4 else 4)
