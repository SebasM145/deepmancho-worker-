"""Cues de otra versión del worker para el banco de mezcla de dj-connect (#1064, paso 3).

El banco de mezcla de la plataforma (`tools/mezcla-banco/`, rama `test/mezcla-banco-fase0`)
renderiza las transiciones de una lista con los cues que hoy están en la base. Para medirlo
ANTES y DESPUÉS de un cambio del worker (#68 cue A, #69 cue B) sin desplegar ni reanalizar en
producción, esto recalcula los cues de cada tema con el worker de una rama y deja una caché
VARIANTE al lado de la original:

  <salida>/<lista>/filas.json   las mismas filas, con `cue_points` y `cue_source` nuevos
  <salida>/<lista>/pares.json   copia
  <salida>/<lista>/audio        enlace a la carpeta de audio de la caché original
  <salida>/<lista>/cues.json    qué cambió en cada tema (A y B antes y después)

Solo cambian los cues. La rejilla es la de la base (tempo = bpm + bpm_fine; ancla =
first_beat_detected_ms, la que mide el worker), y se sigue el mismo camino que analyze():
detect_cues o plan B, cuantizar_cues y, sin BPM, plan C. Se respeta lo mismo que
`worker-result` (`decidirCues` de `_shared/cuesOrigen.ts`): nunca se pisan los cues manuales,
del plan de generación, heredados ni de Mixed In Key, y un respaldo no reemplaza cues de mejor
origen. `energy_entry`/`energy_exit` quedan como en la base: salen de A y H (ventanas de 30 s)
y no de B.

El audio es el MP3 de escucha (el que baja el banco), no el original que analiza el worker:
corre la versión de `main` para ver cuánto de la diferencia es solo por eso.

Solo lee: no escribe en la base ni sube nada. Las filas y el audio son de la biblioteca de
Germán y la salida no puede quedar dentro del repo.

Uso:
  python bancos/cues_banco_mezcla.py <caché de la lista> <salida> [--rama <ref de git>]

  # 7.6.24 (main), 7.6.25 (#68) y 7.6.26 (#68 + #69), con la caché de «Piso 2»:
  L=~/.cache/dj-connect/mezcla-banco/0a8b6bfd-8250-4dbc-88da-07c553b66f80
  python bancos/cues_banco_mezcla.py $L ~/.cache/dj-connect/mezcla-banco-7.6.24
  python bancos/cues_banco_mezcla.py $L ~/.cache/dj-connect/mezcla-banco-7.6.25 --rama origin/fix/930-cue-a-primer-beat
  python bancos/cues_banco_mezcla.py $L ~/.cache/dj-connect/mezcla-banco-7.6.26 --rama origin/feat/1064-cue-b-sobre-930
  # y en dj-connect, una tabla por variante:
  MEZCLA_BANCO_CACHE=~/.cache/dj-connect/mezcla-banco-7.6.26 npm run mezcla:banco -- --salida docs/medidas/<fecha>-banco-piso2-7.6.26
"""
import argparse
import importlib.util
import inspect
import json
import os
import re
import shutil
import statistics as st
import subprocess
import sys
import tempfile

os.environ.setdefault("WORKER_API_URL", "http://banco.invalid")
os.environ.setdefault("WORKER_SECRET", "banco")
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

# Lo mismo que `_shared/cuesOrigen.ts` de dj-connect (worker-result decide con eso).
INTOCABLES = {"manual", "plan_generacion", "heredada", "mixedinkey"}
RESPALDO = {"tiempo_v1", "grilla_v1"}
RANGO = {"tiempo_v1": 0, "grilla_v1": 1, "computed_v3": 2, "structure_v1": 3}

CUE_A, CUE_B = 0, 1


def origen_de_cues(cues, fallback=None):
    """`origenDeCues`: plan C, plan B, plan A (estructural) o el origen que diga el resultado."""
    if cues and all(isinstance(c, dict) and c.get("origen") == "tiempo" for c in cues):
        return "tiempo_v1"
    if cues and all(isinstance(c, dict) and c.get("origen") == "grilla" for c in cues):
        return "grilla_v1"
    if cues and all(isinstance(c, dict) and c.get("energy") is not None and c.get("confidence") is not None
                    for c in cues):
        return "structure_v1"
    return fallback if isinstance(fallback, str) and fallback else "computed_v3"


def decidir_cues(nuevos, origen_previo, previos, fallback=None):
    """`decidirCues`: (cue_points, cue_source) a guardar, o None si se conservan los de la base."""
    if origen_previo and origen_previo in INTOCABLES:
        return None
    if not isinstance(nuevos, list) or not nuevos:
        return None
    origen = origen_de_cues(nuevos, fallback)
    previos = previos if isinstance(previos, list) else []
    if origen in RESPALDO and previos and origen_previo:
        if RANGO.get(origen_previo, float("inf")) > RANGO[origen]:
            return None
    return nuevos, origen


def rejilla_de_fila(fila):
    """(tempo, ancla en ms) de la fila. Tempo = bpm + bpm_fine; sin BPM, (None, ancla)."""
    try:
        bpm = float(fila.get("bpm") or 0)
    except (TypeError, ValueError):
        bpm = 0.0
    try:
        fino = float(fila.get("bpm_fine") or 0)
    except (TypeError, ValueError):
        fino = 0.0
    ancla = fila.get("first_beat_detected_ms")
    if ancla is None:
        ancla = fila.get("first_beat_offset_ms")
    ancla = float(ancla or 0)
    return (bpm + fino if bpm > 0 else None), ancla


def version_del_worker(texto):
    """'7.6.26' a partir del código de worker.py (la línea del arranque), o '?'."""
    m = re.search(r"DeepMancho worker iniciado \(v(\d+\.\d+\.\d+)", texto or "")
    return m.group(1) if m else "?"


def cargar_worker(rama=None):
    """(módulo, versión). Sin rama, el worker.py de este checkout; con rama, el de esa ref de git.
    Los demás módulos (grid_detect, parecido) salen de este checkout: #68 y #69 solo tocan
    worker.py."""
    if not rama:
        import worker
        with open(os.path.join(RAIZ, "worker.py"), encoding="utf-8") as f:
            return worker, version_del_worker(f.read())
    texto = subprocess.run(["git", "-C", RAIZ, "show", f"{rama}:worker.py"], check=True,
                           capture_output=True, text=True).stdout
    nombre = "worker_" + re.sub(r"\W", "_", rama)
    carpeta = tempfile.mkdtemp(prefix="cues-banco-")
    try:
        ruta = os.path.join(carpeta, nombre + ".py")
        with open(ruta, "w", encoding="utf-8") as f:
            f.write(texto)
        spec = importlib.util.spec_from_file_location(nombre, ruta)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        shutil.rmtree(carpeta, ignore_errors=True)
    return mod, version_del_worker(texto)


def cues_como_analyze(w, y, sr, bpm, fb):
    """Los cues que guardaría analyze() de ese worker con esta rejilla."""
    dur_ms = len(y) / sr * 1000.0
    cues = None
    if bpm and 40 < bpm < 240:
        cues = w.detect_cues(y, sr, bpm, fb) or w.cues_respaldo(y, sr, bpm, fb)
        if cues:
            kw = {}
            # 7.6.25 (#68) suma el primer sonido para el cue A; 7.6.24 no lo recibe.
            if "inicio_ms" in inspect.signature(w.cuantizar_cues).parameters:
                kw["inicio_ms"] = w.inicio_audible_ms(y, dur_ms)
            cues = w.cuantizar_cues(cues, bpm, fb, dur_ms, None, **kw)
    if not cues:
        cues = w.cues_respaldo(y, sr, bpm, fb) or w.cues_por_tiempo(dur_ms)
    return cues


def cargar_audio(ruta, w):
    import librosa
    y, _ = librosa.load(ruta, sr=w.SR, mono=True, duration=w.MAX_DURATION)
    return y


def _cue(cues, num):
    return next((c for c in cues or [] if isinstance(c, dict) and c.get("number") == num), None)


def _pos(c):
    return None if c is None else int(c["positionMs"])


def comparar(fila, nuevos, estado, bpm):
    """Lo que cambió en un tema: A y B antes y después (ms) y cuántos compases se movió B."""
    previos = fila.get("cue_points") or []
    out = {"id": fila["id"], "estado": estado, "cue_source": fila.get("cue_source")}
    for nombre, num in (("a", CUE_A), ("b", CUE_B)):
        antes, ahora = _pos(_cue(previos, num)), _pos(_cue(nuevos, num))
        out[f"{nombre}_antes_ms"], out[f"{nombre}_ahora_ms"] = antes, ahora
    b = _cue(nuevos, CUE_B)
    out["b_en_bajo"] = bool(b and b.get("origen") == "auto")
    if bpm and out["b_antes_ms"] is not None and out["b_ahora_ms"] is not None:
        out["b_compases"] = round((out["b_ahora_ms"] - out["b_antes_ms"]) / (240000.0 / bpm), 1)
    else:
        out["b_compases"] = None
    return out


def _dentro(ruta, raiz):
    rel = os.path.relpath(os.path.realpath(ruta), os.path.realpath(raiz))
    return rel == "." or not rel.startswith("..")


def variante(cache, salida, w, cargar=None, log=print):
    """Escribe la caché variante de `cache` en `<salida>/<lista>` y devuelve lo que cambió."""
    cache = os.path.abspath(os.path.expanduser(cache))
    lista = os.path.basename(cache.rstrip(os.sep))
    destino = os.path.join(os.path.abspath(os.path.expanduser(salida)), lista)
    if _dentro(destino, RAIZ):
        raise SystemExit(f"la salida ({destino}) no puede quedar dentro del repo: el audio no se sube")
    if _dentro(destino, cache) or _dentro(cache, destino):
        raise SystemExit("la salida no puede ser la caché original ni contenerla")
    with open(os.path.join(cache, "filas.json"), encoding="utf-8") as f:
        filas = json.load(f)
    with open(os.path.join(cache, "pares.json"), encoding="utf-8") as f:
        pares = json.load(f)
    cargar = cargar or (lambda ruta: cargar_audio(ruta, w))

    cambios, nuevas = [], []
    for previa in filas:
        fila = dict(previa)
        bpm, fb = rejilla_de_fila(fila)
        ruta = os.path.join(cache, "audio", f"{fila['id']}.mp3")
        if fila.get("cue_source") in INTOCABLES:
            cambios.append(comparar(fila, fila.get("cue_points"), f"intocable ({fila['cue_source']})", bpm))
        elif not os.path.exists(ruta):
            cambios.append(comparar(fila, fila.get("cue_points"), "sin audio", bpm))
        else:
            cues = cues_como_analyze(w, cargar(ruta), w.SR, bpm, fb)
            decision = decidir_cues(cues, fila.get("cue_source"), fila.get("cue_points"))
            if decision is None:
                cambios.append(comparar(fila, fila.get("cue_points"), "se conservan (respaldo)", bpm))
            else:
                fila["cue_points"], fila["cue_source"] = decision
                cambios.append(comparar(previa, decision[0], "recalculados", bpm))
                cambios[-1]["cue_source_ahora"] = decision[1]
        c = cambios[-1]
        log(f"[cues] {fila['id'][:8]} {c['estado']:<24} A {c['a_antes_ms']} → {c['a_ahora_ms']} ms · "
            f"B {c['b_antes_ms']} → {c['b_ahora_ms']} ms"
            + (f" ({c['b_compases']:+g} compases{', entrada del bajo' if c['b_en_bajo'] else ''})"
               if c["b_compases"] else ""))
        nuevas.append(fila)

    os.makedirs(destino, exist_ok=True)
    with open(os.path.join(destino, "filas.json"), "w", encoding="utf-8") as f:
        json.dump(nuevas, f)
    with open(os.path.join(destino, "pares.json"), "w", encoding="utf-8") as f:
        json.dump(pares, f, indent=1)
    with open(os.path.join(destino, "cues.json"), "w", encoding="utf-8") as f:
        json.dump(cambios, f, ensure_ascii=False, indent=1)
    audio = os.path.join(destino, "audio")
    if os.path.islink(audio):
        os.unlink(audio)
    if not os.path.exists(audio):
        # a la carpeta real: una variante hecha sobre otra variante no encadena enlaces
        os.symlink(os.path.realpath(os.path.join(cache, "audio")), audio)
    return cambios


def resumen(cambios, version, log=print):
    rec = [c for c in cambios if c["estado"] == "recalculados"]
    n = len(cambios)
    otros = {}
    for c in cambios:
        if c["estado"] != "recalculados":
            otros[c["estado"]] = otros.get(c["estado"], 0) + 1
    log(f"worker {version} · temas {n} · recalculados {len(rec)}"
        + "".join(f" · {k} {v}" for k, v in sorted(otros.items())))
    da = [abs(c["a_ahora_ms"] - c["a_antes_ms"]) for c in rec
          if c["a_antes_ms"] is not None and c["a_ahora_ms"] is not None]
    if da:
        log(f"A cambió en {sum(d > 0 for d in da)} de {len(da)} · |ΔA| mediana {st.median(da):.0f} ms · "
            f"máx {max(da)} ms")
    db = [c["b_compases"] for c in rec if c["b_compases"] is not None]
    if db:
        movidos = [d for d in db if d]
        log(f"B se movió en {len(movidos)} de {len(db)}"
            + (f" · mediana {st.median(movidos):+g} compases" if movidos else "")
            + f" · B en la entrada del bajo {sum(c['b_en_bajo'] for c in rec)}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cache", help="caché de una lista del banco: <base>/<lista> con filas.json, pares.json y audio/")
    ap.add_argument("salida", help="base de la caché variante (MEZCLA_BANCO_CACHE del banco)")
    ap.add_argument("--rama", help="ref de git del worker.py a usar (por defecto, el de este checkout)")
    a = ap.parse_args(argv)
    w, version = cargar_worker(a.rama)
    cambios = variante(a.cache, a.salida, w)
    resumen(cambios, version)


if __name__ == "__main__":
    main()
