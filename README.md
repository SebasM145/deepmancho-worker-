# DeepMancho · Workers de audio (Railway)

Servicios de procesamiento de audio de **DJConnect / DeepMancho**. Corren en Railway (proyecto `DeepManchoWorker`) y se comunican con la plataforma **solo** a través de las funciones del servidor (`WORKER_API_URL` + `WORKER_SECRET`). No tienen acceso directo a la base.

## Qué corre dónde
| Servicio Railway | Archivo | Dockerfile | Se redespliega solo si cambia |
|---|---|---|---|
| `stems-worker` | `stems_worker.py` | `Dockerfile.stems` | `stems_worker.py`, `Dockerfile.stems`, `requirements-stems.txt` |
| `deepmancho-worker-` | `worker.py` (+ `grid_detect.py`) | `Dockerfile` | `worker.py`, `grid_detect.py`, `Dockerfile`, `requirements.txt` |
| `grid-verifier` | `grid_verifier.py` | `Dockerfile.verifier` | `grid_verifier.py`, `Dockerfile.verifier` |

Cambiar este README, el CHANGELOG, las pruebas o `banco_separacion.py` **no redespliega nada**.

## Flujo de trabajos
1. La plataforma encola (`stem_jobs`, análisis, verificación de rejilla).
2. El worker pide trabajo (`stems-next`, `worker-next`, `grid-verify-next`) cada `POLL_INTERVAL_SECONDS`.
3. Procesa (Demucs, análisis, mediciones) y entrega (`stems-result`, `worker-result`, `grid-verify-result`).
4. La plataforma guarda y muestra. El worker **nunca** escribe directo en la base.

## Variables (solo nombres; los valores están en Railway)
`WORKER_API_URL`, `WORKER_SECRET`, `STEMS_MODEL`, `STEMS_ENGINE` (sin definir = demucs; `hibrido` activa el motor híbrido de la v1.23), `HIBRIDO_DIR`, `HIBRIDO_OTHER`, `HIBRIDO_LOTE`, `HIBRIDO_SOLAPES`, `DEMUCS_SEGMENT`, `OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `TORCH_THREADS`, `MAX_TRACK_MB`, `POLL_INTERVAL_SECONDS`, `RAILWAY_DOCKERFILE_PATH`.
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
2. Correr las pruebas locales (`pytest`). La acción `Pruebas` (`.github/workflows/pruebas.yml`) corre `ruff` y `pytest` en cada PR; no hace falta torch ni demucs.
3. Anotar en `CHANGELOG.md`.
4. Subir a `main`: Railway redespliega **solo** el servicio cuyo archivo cambió.
5. Verificar en los registros de Railway y en la plataforma (la cola avanza).

## Banco A/B de separación (local, no se despliega)
`banco_separacion.py` separa los mismos temas con `demucs` y `hibrido` y los compara con las medidas del worker.
```
pip install -r requirements-stems.txt
python banco_separacion.py temas/*.mp3 --salida banco --tempos tempos.csv   # tempos.csv: archivo,bpm,ancla_ms (opcional)
```
Deja `resultados.csv`, `resumen.txt`, las pistas por motor y `escucha_ciega/<tema>/A|B` (la clave está en `clave_ciega.json`: escuchar antes de abrirla).

## Escala de `loudness_lufs` (importante)
Desde la 7.6.9 (3-oct-2026, decisión de Germán), `loudness_lufs` es el **LUFS integrado BS.1770 en estéreo**, el mismo valor que da cualquier medidor. Hasta la 7.6.8 se medía sobre la mezcla en mono, ≈3,8 dB por debajo («Weekend's Started»: −12,2 en mono contra −8,4 reales). Los objetivos de normalización de la app (`soundChain.ts`) y de la radio (`radio-queue-next`) pasaron de −12,5 a −9 en el mismo despliegue, y los temas analizados antes se reanalizaron en silencio (`origen='reanalisis'`).

**El MP3 de escucha no toca la ganancia:** copia el audio tal cual, como dice el estándar de la plataforma. Si un master viene caliente, sus picos pasan de 0 dBFS (+2,9 dBTP en ese tema). Los cubren los limitadores de la app y de `radio.liq` (Refs dj-connect#320).
