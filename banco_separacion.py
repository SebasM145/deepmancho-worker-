#!/usr/bin/env python3
"""
banco_separacion.py — comparación A/B entre motores de separación (demucs vs híbrido).

NO toca producción: no habla con la API, no sube nada. Trabaja con archivos locales
y reutiliza las MISMAS medidas que calcula stems_worker.py:

  · calidad por pista (calidad_pistas: limpia / con_algo / mezclada)
  · fuga de voz en el instrumental (instrumental = suma de las pistas sin voz):
      - fuga_voz_corr: correlación de la envolvente 1–4 kHz del instrumental con la
        envolvente de la voz "de consenso" (promedio de las voces de todos los motores).
        Es un indicio, no una medida absoluta: si hay instrumentos que siguen a la voz, sube.
      - fuga_voz_db: SOLO si hay pistas de referencia (ver --referencias): energía de la
        voz real que queda en el instrumental, en dB respecto de la voz (más negativo = mejor).
  · desfase de las pistas contra la mezcla (los mp3 de demucs vienen ~25 ms tarde)
  · desvío del bombo (ms) respecto de la rejilla (golpes_de_bombo_s sobre la batería
    separada, contra la rejilla del tema) y deriva (ms cada 8 compases, pendiente del desvío).
  · loops: % de ventanas de 8 compases aprobadas por controlar_loop, por pista, con la
    fase medida en la batería de ese motor (igual que cortar_loops).
  · notas de Basic Pitch en el bajo (midi_notes); con referencia, F1 de notas contra el bajo real.
  · tiempo de reloj y RAM pico de la separación (cada separación corre en un proceso aparte).

Salida (en --salida):
  separado/<motor>/<tema>/<pista>.mp3   lo que produce cada motor (mp3 192k, como producción)
  escucha_ciega/<tema>/A|B/<pista>.wav  para escuchar sin saber qué motor es cuál
  clave_ciega.json                      qué letra es qué motor (no abrir antes de escuchar)
  resultados.csv                        una fila por tema × motor
  resumen.txt                           promedios por motor

Uso:
  python banco_separacion.py temas/*.mp3 --salida banco --tempos tempos.csv
  tempos.csv (opcional): archivo,bpm,ancla_ms   (sin él, el tempo se estima con librosa
  y la rejilla se mide en los bombos del tema completo).
  --referencias DIR: DIR/<nombre-del-tema>/{vocals,drums,bass,...}.wav (p. ej. temas sintéticos o MUSDB).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import resource
import shutil
import subprocess
import sys
import time
from pathlib import Path

# stems_worker exige estas variables al importarse; el banco NO usa la API.
os.environ.setdefault("WORKER_API_URL", "http://banco.local/no-usar")
os.environ.setdefault("WORKER_SECRET", "banco-sin-secreto")

import numpy as np  # noqa: E402

import stems_worker as sw  # noqa: E402

PISTAS = ("vocals", "drums", "bass", "other", "piano", "guitar")
MOTORES = ("demucs", "hibrido")
SR = 22050  # la misma frecuencia que usan las medidas del worker


# ----------------------------------------------------------------------------- separación en proceso aparte
def _separar_en_este_proceso(motor: str, src: Path, outdir: Path):
    """Lo que corre dentro del subproceso: separa y guarda las pistas en outdir/<pista>.mp3."""
    outdir.mkdir(parents=True, exist_ok=True)
    if motor == "hibrido":
        stems = sw.separar_hibrido(src, outdir)            # sin respaldo: si falla, el banco lo dice
        info = sw.info_motor("hibrido", "hibrido")
    else:
        tmp = outdir / "_demucs"
        stems_tmp = sw.run_demucs(src, tmp, sw.modelo_demucs(None))
        stems = {}
        for nombre, p in stems_tmp.items():
            dest = outdir / f"{nombre}.mp3"
            shutil.move(str(p), dest)
            stems[nombre] = dest
        shutil.rmtree(tmp, ignore_errors=True)
        info = sw.info_motor("demucs", sw.modelo_demucs(None))
    yo = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    hijos = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    escala = 1 if sys.platform == "darwin" else 1024      # macOS da bytes; Linux, KB
    info["ram_pico_mb"] = round(max(yo, hijos) * escala / 1e6, 1)
    info["pistas"] = {k: str(v) for k, v in stems.items()}
    print("@@RESULTADO@@" + json.dumps(info), flush=True)


def separar_medido(motor: str, src: Path, outdir: Path) -> dict:
    t0 = time.time()
    proc = subprocess.run([sys.executable, __file__, "--_separar", motor, str(src), str(outdir)],
                          capture_output=True, text=True)
    seg = time.time() - t0
    for linea in proc.stdout.splitlines():
        if linea.startswith("@@RESULTADO@@"):
            info = json.loads(linea[len("@@RESULTADO@@"):])
            info["segundos"] = round(seg, 1)
            info["pistas"] = {k: Path(v) for k, v in info["pistas"].items()}
            return info
    raise RuntimeError(f"{motor} falló ({proc.returncode}): {(proc.stderr or proc.stdout)[-1500:]}")


# ----------------------------------------------------------------------------- medidas
def _banda(y, sr, lo, hi):
    Y = np.fft.rfft(y)
    f = np.fft.rfftfreq(len(y), 1 / sr)
    Y[(f < lo) | (f > hi)] = 0
    return np.fft.irfft(Y, n=len(y))


def _env(y, sr, ms=20):
    n = max(1, int(sr * ms / 1000))
    t = len(y) // n
    return np.sqrt((y[: t * n].reshape(t, n) ** 2).mean(axis=1) + 1e-12) if t else np.zeros(1)


def _corr(a, b):
    m = min(len(a), len(b))
    if m < 8:
        return 0.0
    x, y = a[:m] - a[:m].mean(), b[:m] - b[:m].mean()
    return float((x * y).sum() / (np.sqrt((x ** 2).sum() * (y ** 2).sum()) + 1e-12))


def _alinear(ref, y, sr, max_ms=60):
    """Desfase (muestras) que mejor alinea y con ref (+ = y viene tarde). Los mp3 sin
    cabecera de retardo quedan corridos ~26 ms; así se mide sin castigar eso dos veces."""
    m = min(len(ref), len(y), sr * 20)
    a, b = ref[:m], y[:m]
    k = int(sr * max_ms / 1000)
    n = 1 << int(np.ceil(np.log2(2 * m)))
    c = np.fft.irfft(np.fft.rfft(b, n) * np.conj(np.fft.rfft(a, n)), n)
    c = np.concatenate([c[-k:], c[: k + 1]])
    return int(np.argmax(np.abs(c)) - k)


def _desplazar(y, lag):
    if lag > 0:
        return y[lag:]
    if lag < 0:
        return np.concatenate([np.zeros(-lag, dtype=y.dtype), y])
    return y


def fuga_voz_db(inst, voz_ref, sr):
    """Proyección del instrumental sobre la voz real: 20·log10|α|, α = <inst, v>/<v, v>."""
    lag = _alinear(voz_ref, inst, sr)
    x = _desplazar(inst, lag)
    m = min(len(x), len(voz_ref))
    v, x = voz_ref[:m], x[:m]
    alfa = float(np.dot(x, v) / (np.dot(v, v) + 1e-12))
    return round(20 * np.log10(abs(alfa) + 1e-6), 1), round(lag / sr * 1000, 1)


def desvio_bombo(y_bat, sr, bpm, ancla_ms):
    """Mediana del desvío (ms) de los bombos contra la rejilla, desvío con signo y deriva
    (ms cada 8 compases, pendiente de la recta desvío-tiempo)."""
    golpes = sw.golpes_de_bombo_s(y_bat, sr)
    if len(golpes) < 4:
        return None, None, None, len(golpes)
    beat = 60.0 / bpm
    t = np.array(golpes)
    d = ((t - ancla_ms / 1000.0 + beat / 2) % beat - beat / 2) * 1000.0   # con signo, en ms
    pend = float(np.polyfit(t, d, 1)[0]) if len(t) >= 8 else 0.0            # ms por segundo
    return (round(float(np.median(np.abs(d))), 2), round(float(np.median(d)), 2),
            round(pend * 8 * sw.compas_s(bpm), 2), len(golpes))


def loops_aprobados(audio: dict, bpm: float, fase_ms: float) -> tuple[dict, dict]:
    """controlar_loop en todas las ventanas de 8 compases de cada pista."""
    por_pista, motivos = {}, {}
    for pista, (y, sr) in audio.items():
        total = int(len(y) / sr / sw.compas_s(bpm))
        ok = n = 0
        for desde in range(1, total - sw.LOOP_COMPASES + 2, sw.LOOP_COMPASES):
            n += 1
            aprobado, qc = sw.controlar_loop(y, sr, bpm, fase_ms, desde, pista)
            if aprobado:
                ok += 1
            else:
                clave = qc.get("motivo", "?").split(" (")[0]
                motivos[clave] = motivos.get(clave, 0) + 1
        if n:
            por_pista[pista] = (ok, n)
    return por_pista, motivos


def notas_bajo(path, to_beat):
    """midi_notes del worker, sin el ruido que Basic Pitch imprime por consola."""
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        return sw.midi_notes(path, to_beat, 24, 60)


def f1_notas(est, ref, tol_b=0.25):
    """F1 de notas: misma altura y arranque a ≤ 1/4 de tiempo."""
    if not ref and not est:
        return None
    usados, acierto = set(), 0
    for n in est:
        for i, r in enumerate(ref):
            if i not in usados and r["n"] == n["n"] and abs(r["b"] - n["b"]) <= tol_b:
                usados.add(i)
                acierto += 1
                break
    p = acierto / len(est) if est else 0.0
    r = acierto / len(ref) if ref else 0.0
    return round(2 * p * r / (p + r), 3) if p + r else 0.0


def tempo_y_ancla(src: Path, tempos: dict) -> tuple[float, float, str]:
    nombre = src.name
    if nombre in tempos:
        bpm, ancla = tempos[nombre]
        return bpm, ancla, "tempos.csv"
    import librosa
    y, sr = sw.to_wav_mono(src, SR)
    bpm = float(np.atleast_1d(librosa.beat.beat_track(y=y, sr=sr)[0])[0])
    while bpm < 100:
        bpm *= 2
    while bpm > 160:
        bpm /= 2
    # librosa se equivoca por varios BPM en recortes cortos: se afina con los bombos del tema
    # (el tempo que más concentra los bombos en la misma fase del tiempo).
    ks = np.array(sw.golpes_de_bombo_s(y, sr))
    if len(ks) >= 16:
        candidatos = np.arange(round(bpm) - 6, round(bpm) + 6, 0.01)
        fuerza = [abs(np.mean(np.exp(2j * np.pi * ks / (60.0 / c)))) for c in candidatos]
        bpm = float(candidatos[int(np.argmax(fuerza))])
        # La música de club casi siempre está en BPM entero: si el entero vecino concentra
        # casi igual, se prefiere (los graves del bajo también cuentan como "bombo" y sesgan).
        entero = float(round(bpm))
        f_ent = abs(np.mean(np.exp(2j * np.pi * ks / (60.0 / entero))))
        if f_ent >= 0.9 * max(fuerza):
            bpm = entero
    fase = sw.fase_medida_ms(y, sr, bpm)
    return round(bpm, 2), float(fase or 0.0), "estimado (librosa afinado con los bombos del tema)"


# ----------------------------------------------------------------------------- banco
def medir_tema(src: Path, salida: Path, motores, tempos, referencias: Path | None) -> list[dict]:
    tema = src.stem
    bpm, ancla, origen = tempo_y_ancla(src, tempos)
    sw.BPM_GLOBAL[0] = bpm
    print(f"\n== {tema} · {bpm} BPM · ancla {ancla:.1f} ms ({origen})", flush=True)
    ref_dir = (referencias / tema) if referencias else None
    refs = {}
    if ref_dir and ref_dir.is_dir():
        for p in ref_dir.iterdir():
            if p.stem in PISTAS:
                refs[p.stem] = p

    runs = {}
    for motor in motores:
        out = salida / "separado" / motor / tema
        shutil.rmtree(out, ignore_errors=True)
        print(f"   separando con {motor}…", flush=True)
        runs[motor] = separar_medido(motor, src, out)
        print(f"   {motor}: {runs[motor]['segundos']} s · RAM pico {runs[motor]['ram_pico_mb']} MB", flush=True)

    # Voz de consenso (para el indicio de fuga): promedio de las envolventes de voz de todos los motores.
    voces = []
    for info in runs.values():
        if "vocals" in info["pistas"]:
            y, _ = sw.to_wav_mono(info["pistas"]["vocals"], SR)
            e = _env(_banda(y, SR, 1000, 4000), SR)
            voces.append(e / (e.max() + 1e-9))
    m = min(len(v) for v in voces) if voces else 0
    voz_consenso = np.mean([v[:m] for v in voces], axis=0) if voces else None
    voz_ref = sw.to_wav_mono(refs["vocals"], SR)[0] if "vocals" in refs else None
    notas_ref = None
    _, to_beat = sw.beat_grid(bpm, ancla)
    if "bass" in refs:
        notas_ref = notas_bajo(refs["bass"], to_beat)

    mezcla, _ = sw.to_wav_mono(src, SR)
    filas = []
    for motor, info in runs.items():
        stems = info["pistas"]
        fila = {"tema": tema, "motor": motor, "engine_version": info.get("engine_version"),
                "bpm": bpm, "ancla_ms": round(ancla, 1), "segundos": info["segundos"],
                "ram_pico_mb": info["ram_pico_mb"]}
        # calidad por pista (misma función que producción)
        cal = sw.calidad_pistas(stems)
        for p in PISTAS:
            c = cal.get(p) or {}
            fila[f"calidad_{p}"] = c.get("estado")
            fila[f"corr_{p}"] = c.get("correlacion")
        fila["pistas_limpias"] = sum(1 for c in cal.values() if c.get("estado") == "limpia")
        # instrumental = todo menos la voz
        audio = {p: sw.to_wav_mono(stems[p], SR) for p in PISTAS if p in stems}
        largo = min(len(y) for y, _ in audio.values())
        inst = np.sum([audio[p][0][:largo] for p in audio if p != "vocals"], axis=0)
        if voz_consenso is not None:
            e = _env(_banda(inst, SR, 1000, 4000), SR)
            fila["fuga_voz_corr"] = round(_corr(e / (e.max() + 1e-9), voz_consenso), 3)
        if voz_ref is not None:
            fila["fuga_voz_db"], fila["desfase_inst_ms"] = fuga_voz_db(inst, voz_ref, SR)
        # Desfase de las pistas contra la mezcla original (suma de pistas vs mezcla). Los mp3 de
        # demucs (lameenc, sin cabecera de retardo) llegan ~25 ms tarde: se informa aparte.
        suma = np.sum([audio[p][0][:largo] for p in audio], axis=0)
        lag = _alinear(mezcla, suma, SR)
        fila["desfase_pistas_ms"] = round(lag / SR * 1000, 1)
        # bombo contra la rejilla del tema: tal cual se entrega y descontando ese desfase
        if "drums" in audio:
            yb = audio["drums"][0]
            fila["bombo_desvio_ms"], fila["bombo_desvio_signo_ms"], fila["deriva_ms_8c"], fila["bombos"] = \
                desvio_bombo(yb, SR, bpm, ancla)
            fila["bombo_desvio_alineado_ms"] = desvio_bombo(_desplazar(yb, lag), SR, bpm, ancla)[0]
            fm = sw.fase_medida_ms(yb, SR, bpm)
            fila["fase_bateria_ms"] = round(fm, 2) if fm is not None else None
        else:
            fm = None
        # loops: fase medida en la batería de ESTE motor (igual que cortar_loops)
        fase = fm if fm is not None else ancla
        por_pista, motivos = loops_aprobados(audio, bpm, fase)
        ok = sum(a for a, _ in por_pista.values())
        tot = sum(n for _, n in por_pista.values())
        fila["loops_aprobados"] = ok
        fila["loops_ventanas"] = tot
        fila["loops_pct"] = round(100.0 * ok / tot, 1) if tot else None
        for p, (a, n) in por_pista.items():
            fila[f"loops_{p}"] = f"{a}/{n}"
        fila["loops_rechazos"] = json.dumps(motivos, ensure_ascii=False)
        # notas del bajo (Basic Pitch)
        if "bass" in stems:
            notas = notas_bajo(stems["bass"], to_beat)
            fila["bajo_notas"] = len(notas)
            fila["bajo_rango"] = f"{min(n['n'] for n in notas)}-{max(n['n'] for n in notas)}" if notas else ""
            if notas_ref is not None:
                fila["bajo_f1_ref"] = f1_notas(notas, notas_ref)
                fila["bajo_notas_ref"] = len(notas_ref)
        filas.append(fila)
    return filas, runs


def escucha_ciega(salida: Path, tema: str, runs: dict, clave: dict, rnd: random.Random):
    motores = list(runs)
    rnd.shuffle(motores)
    letras = {}
    for i, motor in enumerate(motores):
        letra = "AB"[i] if len(motores) <= 2 else chr(65 + i)
        letras[letra] = motor
        dest = salida / "escucha_ciega" / tema / letra
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True)
        for pista, p in runs[motor]["pistas"].items():
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(p), "-c:a", "pcm_s16le", str(dest / f"{pista}.wav")], check=True)
    clave[tema] = letras


def resumen(filas: list[dict]) -> str:
    lineas = ["BANCO DE SEPARACIÓN — resumen por motor", "=" * 42]
    num = ["segundos", "ram_pico_mb", "pistas_limpias", "fuga_voz_corr", "fuga_voz_db",
           "desfase_pistas_ms", "bombo_desvio_ms", "bombo_desvio_alineado_ms", "deriva_ms_8c", "loops_pct", "bajo_notas", "bajo_f1_ref"]
    guia = {"fuga_voz_corr": "menor = mejor (indicio)", "fuga_voz_db": "menor = mejor",
            "bombo_desvio_ms": "menor = mejor (tal cual se entrega)",
            "bombo_desvio_alineado_ms": "menor = mejor (sin el desfase del mp3)",
            "desfase_pistas_ms": "0 = pistas alineadas con la mezcla", "deriva_ms_8c": "cerca de 0 = mejor",
            "loops_pct": "mayor = mejor", "pistas_limpias": "mayor = mejor (de 6)",
            "bajo_f1_ref": "mayor = mejor", "segundos": "menor = mejor", "ram_pico_mb": "menor = mejor"}
    temas = sorted({f["tema"] for f in filas})
    lineas.append(f"temas: {len(temas)}")
    for motor in sorted({f["motor"] for f in filas}):
        sub = [f for f in filas if f["motor"] == motor]
        lineas.append(f"\n[{motor}] {sub[0].get('engine_version')}")
        for k in num:
            vals = [float(f[k]) for f in sub if f.get(k) not in (None, "")]
            if vals:
                lineas.append(f"  {k:<18} media {np.mean(vals):9.2f}   mediana {np.median(vals):9.2f}   ({guia.get(k, '')})")
        for p in PISTAS:
            estados = [f.get(f"calidad_{p}") for f in sub if f.get(f"calidad_{p}")]
            if estados:
                cuenta = {e: estados.count(e) for e in sorted(set(estados))}
                lineas.append(f"  calidad {p:<9} {cuenta}")
    lineas.append("\nfuga_voz_corr es un indicio (correlación con la voz de consenso de todos los motores);")
    lineas.append("fuga_voz_db y bajo_f1_ref solo existen con pistas de referencia (--referencias).")
    lineas.append("Escuchar escucha_ciega/ ANTES de abrir clave_ciega.json.")
    return "\n".join(lineas)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("audios", nargs="*", type=Path)
    ap.add_argument("--salida", type=Path, default=Path("banco_salida"))
    ap.add_argument("--motores", default=",".join(MOTORES))
    ap.add_argument("--tempos", type=Path, help="CSV archivo,bpm,ancla_ms")
    ap.add_argument("--referencias", type=Path, help="carpeta con <tema>/<pista>.wav reales")
    ap.add_argument("--semilla", type=int, default=None, help="semilla del sorteo A/B (por defecto, al azar)")
    ap.add_argument("--_separar", nargs=3, metavar=("MOTOR", "SRC", "OUT"), help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a._separar:
        _separar_en_este_proceso(a._separar[0], Path(a._separar[1]), Path(a._separar[2]))
        return
    if not a.audios:
        ap.error("faltan archivos de audio")
    motores = [m.strip() for m in a.motores.split(",") if m.strip()]
    for m in motores:
        if m not in MOTORES:
            ap.error(f"motor desconocido: {m}")
    tempos = {}
    if a.tempos:
        with open(a.tempos, newline="") as f:
            for r in csv.DictReader(f):
                tempos[r["archivo"]] = (float(r["bpm"]), float(r.get("ancla_ms") or 0))
    a.salida.mkdir(parents=True, exist_ok=True)
    rnd = random.Random(a.semilla)
    filas, clave = [], {}
    for src in a.audios:
        try:
            f, runs = medir_tema(src, a.salida, motores, tempos, a.referencias)
            filas.extend(f)
            escucha_ciega(a.salida, src.stem, runs, clave, rnd)
        except Exception as e:  # noqa: BLE001
            print(f"   {src.name}: FALLÓ {e!r}", flush=True)
        # se escribe después de cada tema: si se corta a la mitad, no se pierde lo medido
        columnas = []
        for fila in filas:
            columnas += [k for k in fila if k not in columnas]
        with open(a.salida / "resultados.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=columnas)
            w.writeheader()
            w.writerows(filas)
        (a.salida / "clave_ciega.json").write_text(json.dumps(clave, indent=2, ensure_ascii=False))
    texto = resumen(filas) if filas else "sin resultados"
    (a.salida / "resumen.txt").write_text(texto + "\n")
    print("\n" + texto)


if __name__ == "__main__":
    main()
