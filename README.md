# DeepMancho · Workers de audio (Railway)

Servicios de procesamiento de audio de **DJConnect / DeepMancho**. Corren en Railway (proyecto `DeepManchoWorker`) y se comunican con la plataforma **solo** a través de las funciones del servidor (`WORKER_API_URL` + `WORKER_SECRET`). No tienen acceso directo a la base.

## Qué corre dónde
| Servicio Railway | Archivo | Dockerfile | Se redespliega solo si cambia |
|---|---|---|---|
| `stems-worker` | `stems_worker.py` | `Dockerfile.stems` | `stems_worker.py`, `Dockerfile.stems`, `requirements-stems.txt` |
| `deepmancho-worker-` | `worker.py` (+ `grid_detect.py`) | `Dockerfile` | `worker.py`, `grid_detect.py`, `Dockerfile`, `requirements.txt` |
| `grid-verifier` | `grid_verifier.py` | `Dockerfile.verifier` | `grid_verifier.py`, `Dockerfile.verifier` |

Cambiar este README, el CHANGELOG o las pruebas **no redespliega nada**.

## Flujo de trabajos
1. La plataforma encola (`stem_jobs`, análisis, verificación de rejilla).
2. El worker pide trabajo (`stems-next`, `worker-next`, `grid-verify-next`) cada `POLL_INTERVAL_SECONDS`.
3. Procesa (Demucs, análisis, mediciones) y entrega (`stems-result`, `worker-result`, `grid-verify-result`).
4. La plataforma guarda y muestra. El worker **nunca** escribe directo en la base.

## Variables (solo nombres; los valores están en Railway)
`WORKER_API_URL`, `WORKER_SECRET`, `STEMS_MODEL`, `DEMUCS_SEGMENT`, `OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `TORCH_THREADS`, `MAX_TRACK_MB`, `POLL_INTERVAL_SECONDS`, `RAILWAY_DOCKERFILE_PATH`.
Valores de referencia: `OMP_NUM_THREADS=5`, `DEMUCS_JOBS=4`, `DEMUCS_OVERLAP=0.15`, `DEMUCS_SEGMENT=7`. **No usar `NO_CACHE`**: hace lentísimos los despliegues.

## Reglas aprendidas (no romper)
- **El archivo se llama exacto `stems_worker.py`** (y `worker.py`). Subirlo como `stems_worker (1).py` hace que Railway siga corriendo la versión vieja.
- **El tempo es `bpm + bpm_fine`**: `bpm_fine` es la corrección fina, no un tempo aparte.
- **Los hats se miden con pasa-altos > 3 kHz.**
- **El clasificador confunde palmas con hat abierto** (10 de 13 temas): no usar palmas/hats de `drum_grid` como dato medido hasta corregirlo.
- **`bpm_source` / `key_source` en `metadata` o `manual` no se sobrescriben** con el análisis.
- Toda versión nueva sube `VERSION` y se anota en el CHANGELOG.

## Desplegar una versión
1. Cambiar el código y subir `VERSION`.
2. Correr las pruebas locales (`pytest`).
3. Anotar en `CHANGELOG.md`.
4. Subir a `main`: Railway redespliega **solo** el servicio cuyo archivo cambió.
5. Verificar en los registros de Railway y en la plataforma (la cola avanza).
