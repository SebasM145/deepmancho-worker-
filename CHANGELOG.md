# Historial de versiones · worker.py (análisis)



## grid_verifier 1.1.0 (30-sep-2026) — SIN REINTENTOS INÚTILES (W4)
- Antes de bajar el audio revisa el catálogo: sin BPM, sin ancla o con `duration_seconds` < 30 s se reporta al instante, sin descarga.
- La medida se valida con los mismos rangos que `grid-verify-result` (vacía, no finita o fuera de rango): antes llegaba y la plataforma respondía 400 `bad_measurement`.
- `no_bpm`, `no_anchor`, `too_short`, `no_fit` y `bad_measurement` se reportan como `determinista:<código>`. Para cortar los 3 intentos, `finish_grid_verify_job` tiene que respetar ese prefijo (pedido a Funciones en #30).
- Pruebas: `tests/test_grid_verifier.py`, con un tema sintético medido de punta a punta.

## 7.5.2 (30-sep-2026) — LA COLA NO SE TRABA
- Una pista muda colgaba `detect_tempo`: librosa daba 0 BPM y `while seed_bpm < 90: seed_bpm *= 2` no terminaba. Colgó las 3 réplicas con pistas «Piano» vacías (la cola quedó 1 h sin avanzar). Ahora 0 o no finito → 126.
- `analyze` detecta silencio (pico < −80 dBFS) y devuelve `pista_vacia` sin analizar.
- Tope de 10 min por trabajo (SIGALRM): si algo se cuelga, falla ese tema y la réplica sigue.
- En la base, `claim_analysis_job` ya no toma temas en la papelera ni pistas vacías, y corta a los 3 intentos.
## stems_worker 1.22.3 (30-sep-2026) — PISTAS SIN RETRASO
- Demucs ya no escribe MP3 con `--mp3` (lameenc): ese codificador no guarda el retardo y las pistas llegaban **1105 muestras (25 ms) tarde** respecto de la mezcla.
- Ahora demucs entrega WAV y ffmpeg (libmp3lame 192k, cabecera Xing/LAME) lo codifica, la misma cadena que la mezcla: cualquier reproductor trata igual canción y pistas.
- Medido con demucs real sobre 20 s: antes 1105 muestras de atraso, ahora 0.
- Las pistas ya separadas conservan el atraso: los loops lo compensan con `fase_medida_ms`; para el resto conviene volver a separar las que se usen.
## 7.5.1 (30-sep-2026) — RAM OCIOSA
- Medido en Railway (7 días): 3 réplicas con 11,9 GB de RAM promedio (máx. 22,6) y 0,6 vCPU: ~US$130/mes, 90 % RAM ociosa. glibc no devolvía los picos de cada análisis.
- `liberar_memoria()` (gc + `malloc_trim(0)`) después de cada trabajo, del examen y de cada render de set; `MALLOC_ARENA_MAX=2` en el Dockerfile.
- **Master MP3 320k:** el worker ignoraba `master_upload` y los WAV/AIFF/FLAC de subidas masivas quedaban sin convertir. Ahora, con `needs_master_conversion`, sube el master en MP3 320k y `worker-result` cambia `audio_asset_path` y borra el original. Las canciones del Estudio no pasan por aquí (conservan WAV).
- Objetivo: < 1 GB ocioso por réplica. Verificar con las métricas de Railway 24 h después del despliegue.

## 7.5 (30-sep-2026) — NINGÚN TEMA SIN HOT CUES
- Plan A sin cambios: `detect_cues` (metodología MIK sobre el ancla definitiva). En la biblioteca de DJ da 8 cues en 1.109 de 1.113 temas.
- **Plan B** `cues_respaldo`: si el detector no encuentra estructura (menos de 24 compases, poco contraste), cues sobre la rejilla de frases desde el ancla real: A en 0, H en la última frase que deja cola (16 compases o ¼ del tema, prefiriendo caída de energía) y hasta 6 intermedios en los bordes con más cambio medido. Frase de 8 compases (4 o 2 en audio corto).
- **Plan C** `cues_por_tiempo`: sin BPM, A en 0 y H al 85 %.
- B y C van con `confidence` < 0.5 y `origen` (`grilla` / `tiempo`): el mezclador no los usa para mezcla automática; el DJ puede saltar a ellos o corregirlos a mano.
- El resultado incluye `duration_seconds` (si el audio no se cortó en MAX_DURATION): muchos temas generados no la tenían.
- Pruebas: `tests/test_cues_respaldo.py` (boceto de 16 compases, rejilla de frase, audio mínimo, sin BPM, energía 1–10).

# Historial de versiones · stems_worker.py

## 1.22 (27-sep-2026) — MOTOR DE RENDER
- El tema completo desde su ficha (`render-next` / `render-result`): colocación por compás con la fase medida, estéreo o paneo, entrada gradual, paneo móvil, tiro de eco, limpieza de graves, respiro con el bombo (80 ms) y un bus de sala.
- Máster de club: sonoridad objetivo (−9 por defecto) con limitador por bloques y techo −1 dBFS; LUFS reales medidos con ffmpeg (`lufs_of`); WAV 24 bits + MP3 320 kbps subidos a URLs firmadas.
- Prioridad: el render va primero (un DJ lo espera en pantalla), después la separación y el corte de loops.

## 1.21 — afinación medida en cents; tonalidad con esa referencia

## 1.20 (27-sep-2026)
- Tonalidad medida en cada loop no percusivo (Krumhansl-Schmuckler sobre croma, numpy puro) → `qc.key_detectada` y `qc.key_conf`.
- Se considera VERIFICADA solo con `key_conf >= 0.1`. Los bajos con raíz y quinta (sin tercera) son ambiguos por naturaleza: la tonalidad del tema se decide con acordes y melodía.
- Se compara por número de Camelot: las relativas (8A/8B) usan las mismas notas y mezclan sin transportar.

## 1.19.1 – 1.19.3
- Empalme contra la continuación del tema original · alineación medida solo en el bombo (< 150 Hz) · fase medida en los bombos de la pista de batería (la del tema completo difería 40 ms en promedio).

## 1.19 (26-sep-2026)
- Corte de loops de 4/8 compases por pista, con control: alineación del bombo (±5 ms, corrige el desfase constante), deriva de tempo (≤ 5 ms en 8 compases), empalme (fin/inicio ≤ 1,5 con fundido de 2 ms), pista presente sin huecos.
- Pide trabajos a `loops-next` y entrega a `loops-result` SOLO cuando no hay separaciones pendientes (la separación tiene prioridad).
- Fase de la rejilla medida (no el "primer golpe"): corrige los loops "corridos" y los que "se pegan".
- Detector de golpes en numpy puro (ventana 256 / paso 32): desvío medido 2,9 ms con la fase correcta; 34,9 ms si se corta desde el cero (se rechaza).
- Roles: bajo, batería, atmósfera (sintes en partes de poca energía), melodía (la pista armónica con más notas) y acordes.
- Pendiente: guardar la tonalidad detectada aunque haya dato manual.
- Limpieza del repositorio: se retiran los archivos duplicados de subidas anteriores.

## 1.18
- Calidad por pista (`calidad_pistas`: limpia / con_algo / mezclada), notas sueltas y resumen del sampler → `stem_quality`, `sampler`, `vocal_transcript`.

## 1.16 · 1.15
- Versiones anteriores (reemplazadas).
