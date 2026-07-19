# DeepMancho — Worker de análisis (server-side, para Lovable Cloud)

Analiza cada canción con **librosa** (BPM, tonalidad→Camelot, beatgrid, waveform peaks/RMS/bandas,
8 cue points "DeepMancho Standard", energía 1-10) **sin cargar el navegador del DJ** y con mejor
calidad que el análisis en Web Audio.

## Cómo encaja con Lovable Cloud (importante)
Con Lovable Cloud NO administras un Supabase directo, así que **el worker no usa ninguna key de
Supabase**. En su lugar habla con **dos Edge Functions** (ya creadas en tu app) que sí tienen
acceso interno a la base y al Storage:

```
Subida (modo masivo) ──> inserta music_track + analysis_jobs(pending)

Worker (este repo) ──POST /worker-next──>  reclama job + te da una URL firmada del audio
   │  (autenticado con x-worker-secret = WORKER_SECRET)
   ├── descarga el audio de esa URL
   ├── analiza con librosa
   └──POST /worker-result──> escribe waveform/cues/energy en el track + cierra el job
```
El worker solo necesita: **la URL de las Edge Functions** y **el secreto WORKER_SECRET**. Nada más.

## Paso 1 — Crear el secreto en Lovable Cloud
1. En tu app (Lovable) → **Cloud → Secrets → Add secret**.
2. Nombre: `WORKER_SECRET`  ·  Valor: una cadena larga y aleatoria (ej. genera 40+ caracteres).
3. Guarda. (Este mismo valor lo pondrás en el worker.)

## Paso 2 — Conseguir la URL de las funciones
En **Cloud → Edge functions** verás las funciones `worker-next` y `worker-result` con su URL.
La base es: `https://TU-PROYECTO.supabase.co/functions/v1`
(todo antes de `/worker-next`). Esa base es tu `WORKER_API_URL`.

## Paso 3 — Desplegar el worker
### Railway (lo más simple)
1. Sube esta carpeta a un repo de GitHub (el `Dockerfile` en la raíz).
2. railway.app → New Project → Deploy from GitHub repo → elige el repo. Detecta el Dockerfile.
3. Servicio → **Variables**:
   - `WORKER_API_URL` = https://TU-PROYECTO.supabase.co/functions/v1
   - `WORKER_SECRET`  = (el mismo valor del Paso 1)
   - (opcional) `POLL_INTERVAL_SECONDS=5`
4. Deploy. Es un worker de fondo (sin puerto HTTP; si pide healthcheck, déjalo sin él).
   En Logs verás: `DeepMancho worker iniciado. Esperando jobs...`

### Fly.io
```bash
fly launch --no-deploy
fly secrets set WORKER_API_URL="https://TU-PROYECTO.supabase.co/functions/v1" WORKER_SECRET="..."
fly deploy && fly logs
```

## Paso 4 — Probar
En la app → Biblioteca → **"Analizar en servidor"** (encola las canciones sin waveform), o sube en
modo masivo. El worker toma los jobs y llena waveform/cues/energía solo. Verás el progreso en los
logs del worker y el badge "⏳ Analizando (servidor)" en la app.

## Probar localmente
```bash
pip install -r requirements.txt      # requiere ffmpeg instalado en el sistema
export WORKER_API_URL=... WORKER_SECRET=...
python -u worker.py
```

## Escalar
Sube varias réplicas (Railway: replicas; Fly: `fly scale count N`). El reclamo de jobs es atómico
(`FOR UPDATE SKIP LOCKED`), así que varios workers no chocan. Para 700 tracks, 2-3 réplicas los
analizan en minutos.

## Seguridad
- `WORKER_SECRET` va SOLO en las variables del worker y en los Secrets de Lovable — nunca en el repo.
- El worker no abre puertos entrantes y no tiene keys de base de datos.

## Fase 2 (opcional) — Identificación por huella acústica (AcoustID)
El worker puede identificar canciones que subiste **sin ningún tag** (ni artista ni título),
usando huella acústica: **Chromaprint** (`fpcalc`, ya incluido en el Dockerfile vía
`libchromaprint-tools`) genera la huella y **AcoustID** la traduce a artista + título. El worker
solo intenta identificar cuando el track NO trae artista/título, y el backend (`worker-result`)
escribe esos campos únicamente si están vacíos (nunca sobreescribe).

Para activarla:
1. Consigue una **API key gratis** en https://acoustid.org/new-application
2. Agrega la variable de entorno del worker: `ACOUSTID_API_KEY=<tu-key>` (Railway → Variables).
3. Redespliega el worker (el Dockerfile ya instala `fpcalc`).

Si dejas `ACOUSTID_API_KEY` vacía o falta `fpcalc`, el worker sigue funcionando igual, solo que sin
identificar canciones sin tags. Una vez identificado artista+título, puedes completar género/sello/
año/carátula con el botón **"Completar metadatos"** de la Biblioteca (enriquecimiento por texto).

## Renditions AAC (importante para AIFF)
El navegador NO reproduce AIFF. Cuando una canción no tiene versión reproducible (`stream_asset_path`
vacío), el worker la **convierte a m4a/AAC** con ffmpeg y la sube vía una URL firmada que le da la
Edge Function `worker-next`; luego `worker-result` fija `stream_asset_path` y la canción ya aparece en
la radio y el mezclador. Esto pasa automáticamente al analizar en servidor (no requiere config extra;
ffmpeg ya está en el Docker). Para procesar las AIFF ya subidas: en la Biblioteca → "Analizar en
servidor" (encola las que no tengan waveform o rendition).

## Archivos
- `worker.py` — el worker (análisis + rendition AAC + huella acústica opcional + I/O vía Edge Functions).
- `Dockerfile` (incluye ffmpeg + fpcalc), `requirements.txt`, `.env.example`.
- `migration.sql` — referencia de la tabla `analysis_jobs` + RPC (YA aplicada en tu app por Lovable).
