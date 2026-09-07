Servicio de stems y patrones — deepmancho-worker- / stems
Tres archivos al repo SebasM145/deepmancho-worker- (rama main), junto a grid_verifier.py:
stems_worker.py
Dockerfile.stems
requirements-stems.txt
Crear el servicio en Railway (una vez)
Proyecto DeepManchoWorker → New Service → GitHub repo deepmancho-worker-. Settings del servicio:
Ajuste	Valor
Nombre	stems-worker
Dockerfile path	Dockerfile.stems
Watch paths	stems_worker.py, Dockerfile.stems, requirements-stems.txt
Start command	python -u stems_worker.py
Replicas	1 (subir a 2–3 solo si la cola crece)
Memoria	mínimo 4 GB (Demucs en CPU); con htdemucs (4 stems) alcanza con 3 GB
Variables (Settings → Variables):
Variable	Valor
WORKER_API_URL	la misma del worker (https://<proyecto>.supabase.co/functions/v1)
WORKER_SECRET	referencia al del worker: ${{deepmancho-worker-.WORKER_SECRET}}
POLL_INTERVAL_SECONDS	15
STEMS_MODEL	htdemucs_6s (voz, batería, bajo, resto, piano, guitarra) o htdemucs (4 stems, más rápido)
DEMUCS_SEGMENT	8 (bajar a 6 si se queda sin memoria)
MAX_TRACK_MB	60
Qué esperar
Una canción de 6 min tarda 5–10 min en CPU con 6 stems; 3–6 con 4 stems.
El primer arranque no descarga modelos: vienen en la imagen.
Log por job: duración, bpm, ancla → stems subidos → notas de bajo, compases, acordes.
Si un job falla, se reporta a stems-result con ok=false y el error; no se reintenta solo.
Cómo verificar que funciona
En la app, "Separar en pistas" sobre una canción propia.
Logs del servicio: debe verse demucs: -n htdemucs_6s ..., luego subido vocals … y listo: 6 stems, N notas de bajo, M compases.
En la base: select stem, duration_seconds, lufs from track_stems where track_id='…' y select jsonb_array_length(bass_midi), (drum_grid->>'bars')::int from track_patterns where track_id='…'.
Qué NO hace a propósito
No escribe en music_tracks, ni en cues, ni en rejilla. No compite con worker.py.
No descarga los stems a ningún lado público: solo los sube al bucket privado con la URL firmada que le entrega stems-next.
