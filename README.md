DeepMancho — Worker de análisis de audio

Versión: v7.3 (25 de agosto de 2026)

Analiza cada canción con librosa/scipy (BPM, tonalidad → Camelot, rejilla de beats, waveform, 8 cue points por estructura musical, LUFS, energía) sin cargar el navegador del DJ y con mejor calidad que el análisis en Web Audio.

Cómo encaja con Lovable Cloud

Con Lovable Cloud no se administra Supabase directamente, así que el worker no usa ninguna key de base de datos. Habla con dos Edge Functions que sí tienen acceso interno:

Subida (modo masivo) ──> inserta music_track + analysis_jobs(pending)

Worker (este repo) ──POST /worker-next──> reclama job + URL firmada del audio
   │  (autenticado con x-worker-secret = WORKER_SECRET)
   ├── descarga el audio
   ├── analiza con librosa/scipy
   └──POST /worker-result──> escribe resultado y cierra el job

El worker solo necesita la URL de las funciones y el secreto. Nada más.

Configuración
Variable	Qué es
WORKER_API_URL	Base de las Edge Functions: https://TU-PROYECTO.supabase.co/functions/v1
WORKER_SECRET	Secreto compartido (Lovable → Cloud → Secrets). Solo en variables de entorno, nunca en el repo
POLL_INTERVAL_SECONDS	Cada cuánto sondea trabajos (recomendado: 30)
ENABLE_ANCHOR_BACKFILL	Habilita el cálculo del ancla de rejilla
ENABLE_MIX_V7	Dejar apagado. Reactiva un MIX-IN/OUT experimental que fue refutado con medición
ACOUSTID_API_KEY	Opcional: identificación por huella acústica
Despliegue (Railway)

El servicio está conectado al repo de GitHub. Basta con hacer push: Railway detecta el Dockerfile (Python 3.11 + ffmpeg) y despliega solo.

bash
git add worker.py
git commit -m "worker vX.Y: ..."
git push

Verificación en los logs:

DeepMancho worker iniciado (v7.3: ancla al ATAQUE del kick + CUES POR
ESTRUCTURA MUSICAL ...). Esperando jobs...
[CM2 EXAMEN] ... RESULTADO: APROBADO ✅

Si el banner muestra una versión anterior, el push no llegó o Railway sirvió un build viejo.

No usar railway up desde una carpeta local salvo que contenga el Dockerfile: sin él, Railway cae a Python 3.13 y scipy no compila.

Escalar

Varias réplicas trabajan en paralelo sin pisarse: el reclamo de trabajos es atómico (FOR UPDATE SKIP LOCKED). Hoy corre con 3 réplicas.

Qué hace el worker
Rejilla y ancla (v7.2+)

El ancla se calcula sobre el ataque del kick, no sobre el pico de la envolvente: banda 35–130 Hz (Butterworth de fase cero) + envolvente de Hilbert + cruce del 25 % de la altura del pico. El downbeat se desambigua con la banda de caja (1.5–5 kHz, que cae en los tiempos 2 y 4), votando solo compases completos.

Por qué: con el método anterior, 9 de 24 canciones auditadas tenían el ancla corrida −77 a −69 ms con rejilla y BPM perfectos. El detector actual fue validado por medición independiente: coincidió en 5 de 6 tracks dentro de ±5 ms.

El worker reporta grid_confidence (0–1) para que la app sepa cuánto fiarse.

Cue points (v7.3)

8 cues por estructura musical medida, según la regla completa en REGLA-HOT-CUES.md (incluida en este repo). Cinco reglas duras:

R1 El MIX-IN nunca es el compás 0 (prohibido copiar el ancla de la rejilla)
R2 Todo cue en múltiplo de 4 compases; los de mezcla en múltiplo de 16
R3 El MIX-OUT es el inicio del outro, no el último drop, y deja ≥16 compases de cola mezclable
R4 Al menos 2 de los 8 cues en la segunda mitad del track
R5 Cada cue lleva confidence 0–1; con confianza <0.5 no se usa para mezcla automática

Por qué se reescribió: la versión anterior (computed_v3) era una plantilla proporcional, no análisis. Medido sobre 15 tracks: cue[0] era literalmente una copia del ancla en 15 de 15, los saltos entre cues eran múltiplos exactos de {8,16,24,32}, y solo el 44 % coincidía con un borde de sección real.

Renditions — MP3 192k (cambió en v7.3)

Cuando una canción no tiene versión reproducible (ej. AIFF), el worker la convierte al formato estándar de la plataforma:

MP3 (libmp3lame) · CBR 192 kbps · 44.1 kHz · estéreo, con -map_metadata 0, -id3v2_version 3 y -write_xing 1.

Por qué MP3 y no AAC: el contenedor MP4 depende del átomo moov y produjo archivos corruptos en producción → DEMUXER_ERROR_NO_SUPPORTED_STREAMS y "Media failed to decode" en iOS Safari. MP3 no tiene contenedor frágil, decodifica en todo el parque (Safari/PWA, Web Audio, Liquidsoap) y en CBR da seek determinístico, que es lo que necesitan el beatgrid y los hot cues.

Reglas del transcode:

Sin loudnorm ni volume. loudness_lufs se mide sobre el audio y la normalización se aplica en reproducción. Normalizar el archivo rompería esa medición y podría introducir clipping en los picos de graves.
Metadata preservada (título, artista, BPM, key). La fuente de verdad sigue siendo bpm_source/key_source en base.

El path resultante va a stream_mp3_asset_path. El campo stream_asset_path (AAC) queda legacy: los tracks que ya lo tienen lo conservan y se siguen sirviendo, pero no se genera más.

Catálogo existente: no re-transcodificar. Re-encodear de comprimido a comprimido degrada calidad y no ahorra bytes.

Identificación por huella acústica (opcional)

Con ACOUSTID_API_KEY, el worker identifica canciones sin tags vía Chromaprint (fpcalc, ya en el Dockerfile) + AcoustID. Solo lo intenta cuando el track no trae artista/título, y el backend escribe esos campos únicamente si están vacíos. Sin la key, el worker funciona igual.

Reglas que el worker NUNCA rompe
Fuentes de verdad protegidas: un valor con bpm_source/key_source en metadata o manual jamás se sobrescribe por análisis. Igual con grid_source='manual'.
El ancla detectada se escribe en first_beat_detected_ms; que llegue o no a first_beat_offset_ms lo decide el backend según la fuente de verdad.
Sin normalización en el transcode.
Un residuo de rejilla alto (>8 ms) no escribe ancla — mejor ningún dato que un dato malo.
Antes de un backfill masivo

Ningún recálculo del catálogo se lanza sin pasar el criterio de aceptación de REGLA-HOT-CUES.md §7:

≥80 % de los cues B→H coincidiendo con un borde de sección real
0 tracks con MIX-IN en el primer 5 % de la duración
0 tracks con MIX-OUT dejando menos de 16 compases de cola
Prueba de oído sobre al menos 3 tracks

Precedente: la v7.1 traía un MIX-IN/MIX-OUT nuevo que "sonaba razonable"; se midió sobre 25 tracks y era peor que el anterior. Quedó apagada tras ENABLE_MIX_V7. Medir primero, aplicar después.

Probar localmente
bash
pip install -r requirements.txt      # requiere ffmpeg en el sistema
export WORKER_API_URL=... WORKER_SECRET=...
python -u worker.py
Archivos
Archivo	Qué es
worker.py	El worker completo
grid_detect.py	Detección de rejilla de beats
Dockerfile	Python 3.11 + ffmpeg + fpcalc
requirements.txt	Dependencias con versiones fijas
REGLA-HOT-CUES.md	Regla normativa de colocación de cues
Seguridad
WORKER_SECRET va solo en variables de entorno, nunca en el repo.
El worker no abre puertos entrantes y no tiene keys de base de datos.
Si el secreto queda expuesto (por ejemplo en una captura de pantalla), rotarlo en tres lugares a la vez: Secrets de Lovable, .env del VPS de radio, y variables de Railway.
