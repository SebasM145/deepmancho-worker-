"""#878 · Mezcla preparada en el servidor, fase 2: el worker arma la escucha por TRAMOS.

Contrato con la plataforma (función `escucha-tramo`, x-worker-secret; el worker no escribe en la base):
  POST ?action=next   -> {job: null, sesion_viva} | {job: {id, clave, plan, intentos}, temas: [desde, hasta] | [hasta],
                          prefijo, sesion_viva: true}         temas[i] = metadatos + audio_url firmada
  POST ?action=subir  -> {job_id, intentos, nombre} -> {url, token, path}   PUT del archivo a `url`
  POST ?action=result -> {job_id, intentos, duracion_s, inicios: [{track_id, s}]}   exige indice.m3u8 subido
  POST ?action=fail   -> {job_id, intentos, error}

Un tramo = una transición (escucha-preparar): el tema `desde` desde que terminó la transición anterior (`plan.previa`;
sin previa, desde el principio) hasta que el tema `hasta` suena solo y a su tempo después de `plan.transicion`. El
último tema de la lista es un tramo de cierre (un solo tema, sin transición) hasta el final del audio.

Los bordes caen en una muestra EXACTA del tema, en la parte donde suena solo y a su tempo (pasada la rampa de tempo, el
cruce, los graves y la cola del eco, más MARGEN_SOLO_S): el final de un tramo y el principio del siguiente son el mismo
audio, así que pegarlos es exacto. Las cifras de cada transición son las de `_mezcla_plan` (las del set, #143).

Nivel: sin normalizar por tramo (cambiaría el volumen en cada borde). Solo un limitador fijo a -1 dBFS (la suma de dos
temas en un cruce puede pasar de 0 dBFS), con PREROLL_S de audio antes del borde para que su estado sea el mismo de los
dos lados; el pre-roll se descarta. Formato: HLS VOD por tramo, AAC 192 kbps en fMP4 (init.mp4 + seg_NNNN.m4s) de ~6 s.
"""
import os
import shutil
import subprocess
import tempfile
import time

import numpy as np
import requests

PREROLL_S = 2.0
MARGEN_SOLO_S = 0.5
SEG_S = 6
AAC_KBPS = 192
LIMITE = 10 ** (-1.0 / 20.0)        # -1 dBFS
COLA_ECO_TIEMPOS = 8.5               # cola_eco: 8 tiempos + el medio tiempo del envío
MAX_TRAMO_S = 1800                   # el mismo tope de escucha-tramo (result)
ESPERA_VIVA_S = 3.0                  # con alguien escuchando, el worker no espera más que esto entre vueltas
ESCUCHA_DISPONIBLE = True
_REINTENTO_EN = 0.0                  # si la función falla, no se le vuelve a preguntar hasta esta hora
TIPOS_CONTENIDO = {".m3u8": "application/vnd.apple.mpegurl", ".mp4": "audio/mp4", ".m4s": "audio/mp4"}


class TramoInvalido(RuntimeError):
    """El plan no se puede armar por tramos (por ejemplo, un tema demasiado corto)."""


def _w():
    import worker  # import tardío: worker importa este módulo
    return worker


def punto_solo(t, bpm_desde):
    """Segundo del tema `hasta` desde el que suena solo y a su tempo después de la transición `t` (None sin `t`)."""
    if not t:
        return None
    num = _w()._num
    entrada = max(0.0, num(t.get("entrada_seg"), 0.0))
    beat = 60.0 / bpm_desde if bpm_desde and bpm_desde > 0 else 0.5
    tipo = t.get("tipo")
    if tipo == "mezcla":
        dur = max(0.0, num(t.get("duracion_seg"), 0.0))
        rate = num(t.get("rate"))
        if rate and abs(rate - 1.0) >= 1e-4 and dur > 0:
            fin = entrada + dur * rate + max(0.0, num(t.get("release_seg"), 0.0)) * (1.0 + rate) / 2.0
        else:
            fin = entrada + dur
        # Si no se puede estirar, _mezcla_plan pasa la mezcla con eco: el borde queda también después de esa cola.
        fin = max(fin, entrada + COLA_ECO_TIEMPOS * beat)
    elif tipo == "eco":
        fin = entrada + COLA_ECO_TIEMPOS * beat
    else:  # corte o encadenado: la entrante arranca entera en `entrada`
        fin = entrada
    return fin + MARGEN_SOLO_S


def _bpm(tema):
    w = _w()
    return w._num(tema.get("bpm"), 0.0) + w._num(tema.get("bpm_fine"), 0.0)


def armar_pcm(plan, temas, tmpdir):
    """PCM del tramo (float32 (n, 2) a SET_SR) con PREROLL_S de audio antes del borde. Devuelve (pcm, preroll, inicios)."""
    w = _w()
    sr = w.SET_SR
    previa, tr = plan.get("previa"), plan.get("transicion")
    if len(temas) == 1:  # cierre: el último tema, desde donde terminó la transición anterior hasta el final
        tema = temas[0]
        p = os.path.join(tmpdir, "cierre.audio")
        w._bajar_tema(tema.get("audio_url"), p)
        audio = w._decode_pcm(p)
        ini = int(round((punto_solo(previa, _bpm(tema)) or 0.0) * sr))
        fin = w.fin_util(audio)
        if ini >= fin:
            raise TramoInvalido("el cierre empieza después del final del tema")
        pre = min(int(PREROLL_S * sr), ini)
        return audio[ini - pre:fin].copy(), pre, [{"track_id": w._id_de(tema), "s": 0.0}]

    if not tr or len(temas) != 2 or not w.plan_valido(temas, [tr]):
        raise TramoInvalido("el plan no coincide con los temas del tramo")
    a, b = temas
    largos = []
    salida, tracklist = w._mezcla_plan(temas, [tr], tmpdir, largos=largos)
    total = len(salida)
    corte = int(round(tracklist[1]["start_seconds"] * sr))
    ini = int(round((punto_solo(previa, _bpm(a)) or 0.0) * sr))
    if ini >= corte:
        raise TramoInvalido("el tema es demasiado corto: la transición siguiente empieza antes de que termine la anterior")
    # La línea de `hasta` termina igual que su audio: la muestra s del tema está en total - (largo - s).
    fin = total - (largos[1] - int(round(punto_solo(tr, _bpm(a)) * sr)))
    fin = max(corte + 1, min(fin, total))
    pre = min(int(PREROLL_S * sr), ini)
    pcm = np.asarray(salida[ini - pre:fin], dtype=np.float32).copy()
    inicios = [{"track_id": w._id_de(a), "s": 0.0}, {"track_id": w._id_de(b), "s": round((corte - ini) / sr, 3)}]
    return pcm, pre, inicios


def codificar_hls(pcm, preroll, carpeta):
    """Limitador fijo, sin el pre-roll, a HLS fMP4 (init.mp4 + seg_NNNN.m4s + indice.m3u8) en `carpeta`.
    Devuelve (nombres en orden de subida, duración en s)."""
    w = _w()
    os.makedirs(carpeta, exist_ok=True)
    cruda = os.path.join(carpeta, "..", "tramo.f32")
    np.ascontiguousarray(pcm, dtype=np.float32).tofile(cruda)
    filtro = (f"alimiter=limit={LIMITE:.6f}:attack=5:release=50:level=0,"
              f"atrim=start_sample={int(preroll)},asetpts=N/SR/TB")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(w.SET_SR), "-ac", "2", "-i", cruda, "-af", filtro,
         "-c:a", "aac", "-b:a", f"{AAC_KBPS}k", "-f", "hls", "-hls_time", str(SEG_S), "-hls_playlist_type", "vod",
         "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4",
         "-hls_segment_filename", os.path.join(carpeta, "seg_%04d.m4s"), os.path.join(carpeta, "indice.m3u8")],
        check=True, capture_output=True, timeout=600)
    os.remove(cruda)
    segs = sorted(f for f in os.listdir(carpeta) if f.startswith("seg_") and f.endswith(".m4s"))
    if not segs or not os.path.exists(os.path.join(carpeta, "init.mp4")):
        raise RuntimeError("ffmpeg no dejó los segmentos HLS")
    return ["init.mp4", *segs, "indice.m3u8"], (len(pcm) - preroll) / w.SET_SR


def _api(accion, payload=None):
    w = _w()
    r = requests.post(f"{w.WORKER_API_URL}/escucha-tramo?action={accion}", headers=w.HEADERS, json=payload or {}, timeout=60)
    if r.status_code == 404 and accion == "next":
        return None
    r.raise_for_status()
    return r.json()


def _subir(job, carpeta, nombre):
    s = _api("subir", {"job_id": job["id"], "intentos": job["intentos"], "nombre": nombre})
    ext = os.path.splitext(nombre)[1]
    with open(os.path.join(carpeta, nombre), "rb") as f:
        up = requests.put(s["url"], data=f, timeout=300,
                          headers={"Content-Type": TIPOS_CONTENIDO.get(ext, "application/octet-stream"), "x-upsert": "true"})
    if up.status_code >= 400:
        raise RuntimeError(f"subida de {nombre} fallida (HTTP {up.status_code})")


def atender_tramo(data):
    """Arma, codifica y sube un tramo. Lanza si falla (el que llama avisa con fail)."""
    job, temas = data["job"], data.get("temas") or []
    t0 = time.time()
    tmpdir = tempfile.mkdtemp(prefix="dm_tramo_")
    try:
        pcm, pre, inicios = armar_pcm(job.get("plan") or {}, temas, tmpdir)
        if (len(pcm) - pre) / _w().SET_SR > MAX_TRAMO_S:
            raise TramoInvalido("el tramo dura más de 30 min")
        carpeta = os.path.join(tmpdir, "hls")
        nombres, dur = codificar_hls(pcm, pre, carpeta)
        del pcm
        for n in nombres:  # indice.m3u8 al final: escucha-tramo lo exige para dar el tramo por listo
            _subir(job, carpeta, n)
        _api("result", {"job_id": job["id"], "intentos": job["intentos"], "duracion_s": round(dur, 3), "inicios": inicios})
        print(f"[escucha] tramo {job['clave'][:12]}… listo: {dur:.1f} s, {len(nombres) - 2} segmentos, "
              f"{time.time() - t0:.1f} s de trabajo", flush=True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def poll_escucha():
    """Pide un tramo y lo arma. Devuelve (hizo_algo, sesion_viva). Si la función no existe (404) no se vuelve a preguntar
    hasta reiniciar; si falla, se reintenta en 60 s (el análisis sigue)."""
    global ESCUCHA_DISPONIBLE, _REINTENTO_EN
    w = _w()
    if not ESCUCHA_DISPONIBLE or time.time() < _REINTENTO_EN:
        return False, False
    try:
        data = _api("next")
    except Exception as e:
        _REINTENTO_EN = time.time() + 60
        print(f"[escucha] no disponible: {type(e).__name__}", flush=True)
        return False, False
    if data is None:
        ESCUCHA_DISPONIBLE = False
        print("[escucha] la función escucha-tramo no existe todavía: se omite", flush=True)
        return False, False
    viva = bool(data.get("sesion_viva"))
    job = data.get("job")
    if not job:
        return False, viva
    try:
        atender_tramo(data)
    except Exception as e:
        msg = w.sin_firma(e)[:500]
        print(f"[escucha] tramo {str(job.get('clave'))[:12]}…: fallo ({msg[:200]})", flush=True)
        try:
            _api("fail", {"job_id": job["id"], "intentos": job["intentos"], "error": msg})
        except Exception:
            pass
    return True, viva


def espera_con_escucha(segundos, viva):
    """Con alguien escuchando una lista preparada, la vuelta siguiente llega rápido (el tramo siguiente no espera la
    espera creciente del análisis, hasta 120 s)."""
    return min(segundos, ESPERA_VIVA_S) if viva else segundos

