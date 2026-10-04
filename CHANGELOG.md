# Historial de versiones · worker.py (análisis)

## 7.6.12 (4-oct-2026) — 136 BPM SIN ETIQUETA YA NO SALE A 2/3 · Refs dj-connect#206
- **Qué pasaba:** un tema sin BPM en la etiqueta a 136, con bombo en cada tiempo y hi-hat a contratiempo, salía **90,54** (`tempo_stability` «desconocido»). La semilla ×2/3 de 7.6.9 (#17) entraba en la búsqueda (90,7 ≥ 90), y su rejilla de 1,5 tiempos = **3 medios tiempos** caía siempre sobre un golpe (bombo, hi-hat, bombo…): puntuaba más del 10 % sobre el tempo real.
- **Arreglo (`grid_detect.explicada_por_media_rejilla`):** una proporción cuyo período es un múltiplo entero del **medio tiempo** de la mejor octava no compite, porque todo lo que puntúa ya lo explica la octava. «Right Thing» (123 contra la octava 174,33: 2,835 medios tiempos) y los ×4/3 y ×0,75 (1,5 y 2,667) siguen compitiendo como antes.
- Lo tomó el Mezclador por la regla anti-bucle (3 intentos de Workers). Pruebas: `tests/test_206_proporcion_media_rejilla.py` y la de punta a punta de #34, `tests/test_206_analisis_completo.py` (7/7).

## 7.6.11 (4-oct-2026) — TEMAS LARGOS SIN MATAR LA RÉPLICA, Y ETIQUETAS DE BPM FUERA DE RANGO · Refs #206 #573
- **«Gratitude» (631 s) mató la réplica 3 veces** (08:51, 09:00 y 09:09 UTC): «sin respuesta después de 3 intentos», sin «OK» ni «FALLO» en el log. Medido con el archivo real, `analyze` llegaba a **5,8 GB**. El culpable estaba en `refine_bpm`: `beat_track` estima el tempo con un tempograma de autocorrelación de 384 × ~100.000 cuadros (hop 128 a 22 kHz), que solo él sumaba **+6,6 GB**. La métrica de Railway (suma de réplicas, muestreada cada 60 s) no mostraba ese pico.
- **Arreglo:** `tempo_por_bloques` calcula el promedio del tempograma por bloques de cuadros (mismo relleno, ventana y normalización que `librosa.feature.tempogram`) y se lo pasa a `librosa.feature.tempo`; `beat_track` recibe ese tempo. **Mismo resultado:** con Gratitude, el tempo (121,5993) y los 1.221 beats son idénticos; también en temas sintéticos de 98 a 174 BPM. **Pico: 5,8 → 2,8 GB.**
- **«Paris» trae TBPM=240** y la base lo tomó como BPM bloqueado: se analizaba con semilla 240 y CM2 medía el ancla con ese período (no la escribía). Ahora, si el BPM bloqueado está fuera de 70–180, el análisis y CM2 usan su octava (240 → 120) y se agrega la bandera `bpm_etiqueta_octava:240→120`. **No se pisa el BPM guardado:** el contrato protege lo bloqueado. Corregir el valor guardado es de la plataforma (ver el PR).
- Pruebas: `tests/test_temas_largos.py`.

## 7.6.10 (4-oct-2026) — UN REDESPLIEGUE YA NO DEJA TEMAS COLGADOS
- **Qué pasó:** el reanálisis de #320 se encoló a las 00:12:30, mientras Railway rotaba las réplicas a la 7.6.9. Las réplicas que iban a apagarse tomaron 6 temas a las 00:17 y Railway las cortó segundos después. **No fue falta de memoria:** no hubo error de Python, el corte llegó antes del BPM y los 7,7 GB de la métrica son la **suma** de las réplicas viejas y nuevas mientras se solapaban (en reposo: 0,8 GB entre 5). Los 6 quedaron en `processing` hasta que el reclamo los retomó a los 8 min.
- **Ahora:** con SIGTERM a mitad de un trabajo, el worker lo devuelve a la cola (`error` → `worker-result` lo pasa a `pending`), borra el temporal y sale. Sin trabajo en curso, sale limpio. En los dos casos deja de pedir temas nuevos.
- **Para que sirva en Railway:** entre SIGTERM y SIGKILL tiene que quedar tiempo. Hay que revisar el tiempo de drenado del servicio (`RAILWAY_DEPLOYMENT_DRAINING_SECONDS`, por ejemplo 15).
- Cuesta un intento: un tema cortado 3 veces seguidas queda en `error`, y se reintenta desde la Biblioteca.
- Pruebas: `tests/test_apagado.py`.

## 7.6.9 (3-oct-2026) — `loudness_lufs` EN LA ESCALA ESTÁNDAR (BS.1770, estéreo) · Refs #320
- **Lo que se reportó:** «Weekend's Started» da −8,6 LUFS y +2,9 dBFS en su MP3 de escucha, contra `loudness_lufs` = −12,2.
- **Lo que se midió:**
  - **El MP3 no sube el volumen.** El original WAV da −8,4 LUFS y el MP3 −8,6.
  - **La diferencia es de escala:** `loudness_lufs` se medía sobre la mezcla en **mono** (−12,2), 3,8 dB por debajo de BS.1770 en estéreo.
  - Los picos vienen del master: +0,9 dBTP, con 2.502 muestras ya a 0 dBFS. La codificación MP3 los lleva a +2,9.
- **Cambio:** `loudness_lufs` = LUFS integrado BS.1770 en estéreo, lo mismo que mide cualquier medidor. El log muestra las dos escalas durante la transición (`LUFS -8.4 (escala mono de antes: -12.2)`).
- **Va junto con:** los objetivos de normalización calibrados en la escala vieja. Pasan de −12,5 a ≈ −9 en el mismo despliegue:
  - `TARGET_LUFS` de `src/lib/audio/soundChain.ts` (app);
  - `TARGET_LUFS` de `radio-queue-next` (radio).

  Si no se mueven, todo lo analizado con la 7.6.9 suena ~3,5 dB más bajo.
- **Picos:** el MP3 sigue sin ganancia ni limitador, como dice el estándar (`audioConverter.ts` y aquí). La reproducción normaliza y le devolvería al archivo cualquier ganancia que se le quite. El techo va al final de la cadena: la app ya tiene limitador (−3 dB, 20:1) y la radio lo suma en `radio.liq` (Mezclador).
- Pruebas: un seno de 1 kHz a −20 dBFS en L y R mide −20 LUFS; con la escala mono daba −23. Otra prueba compara contra pyloudnorm en estéreo.

## Nota (3-oct-2026) — escala de `loudness_lufs`: primero B, después A el mismo día
- Primero se decidió B (documentar la escala mono, PR #30). Luego Germán pidió A antes del lanzamiento: la 7.6.9 de arriba, con los objetivos de la app y la radio movidos a −9 en el mismo despliegue y el reanálisis en silencio.

## 7.6.8 (3-oct-2026) — EXAMEN CM2 CON GOLDEN SET SINTÉTICO (#573)
- El examen CM2 medía 6 temas del catálogo. Al pasar a la papelera, su audio quedó solo en el proyecto viejo, así que `stream-track` daba 404 y el examen salía **NO APROBADO** en cada arranque: CM2 no escribía anclas.
- Ahora el golden set es **sintético**: 6 temas generados con BPM (122–128) y fase de bombo conocidos y codificados con `make_rendition`, el mismo MP3 que oye el DJ. Así se mide el ancla sobre la rendición, **sin red y sin depender de la música de nadie**.
- Cada tema trae trampas de música real: intro y break sin bombo, bajo a contratiempo dentro de la banda del bombo (35–130 Hz), clap en 2 y 4, hi-hats y ruido de fondo. **Vuelve el tercer par**, que estaba retirado.
- Medido sin codificar: el detector adelanta 4–7 ms en todos los temas (el filtro de fase cero). Los pares quedan a ≤ 3 ms, dentro del criterio de ±10 ms. El examen imprime además el error absoluto de cada tema contra su oro.
- Se retira la prueba ciega (This Sound y Day 'N' Nite, también en la papelera).
- Pruebas: `tests/test_golden_sintetico.py`. Aprueba sin red, **reprueba si el ancla de un tema se corre 25 ms** y reprueba si no se puede codificar.

## stems_worker 1.23.3 (3-oct-2026) — UNA PISTA CASI VACÍA YA NO DESAPARECE DE LA CALIDAD
- `_envolvente` devolvía **2 valores** cuando la pista dura menos de 80 ms, y `calidad_pistas` espera 3. El `ValueError` se tragaba como «no se pudo leer» y la pista **faltaba** en `stem_quality`, en vez de salir como `vacia`. Ahora devuelve las 3 salidas y la pista queda `vacia`.
- Pruebas: `tests/test_calidad_pistas.py` (también fija la filtración del fuerte al débil).

## Errores sin firma en la limpieza de copias y en los sets (3-oct-2026) — W6 · sin versión propia
- Entra con la versión siguiente del worker que se mezcle (no cambia el banner, para no chocar con #21, #22 y #24).
- `stream-limpiar` (#265) mandaba `str(e)` tal cual a la base. Ahora pasa por `sin_firma`.
- `set-render` (#143) usaba solo `_sin_firmas`, que busca `https://…?…`. Con un error de conexión, requests escribe «Max retries exceeded with url: /storage/v1/object/sign/…?token=eyJ…», sin host, y el token pasaba. Ahora se aplica `sin_firma` encima.
- Pruebas: `tests/test_errores_sin_firma.py` (sin el arreglo fallan 3 de 5).

## 7.6.7 (3-oct-2026) — TEMPO MÁS RÁPIDO, MISMO RESULTADO (carga masiva)
- El 60 % de `analyze` se iba en un bucle de Python de `grid_detect._grid_score`: unas 10.700 llamadas en un tema de 10 min, cada una sacando el máximo de una ventana de 3 cuadros golpe por golpe. Ahora la ventana se calcula una sola vez (`maximum_filter1d`) y cada llamada es una indexación de numpy.
- **Mismo resultado, bit a bit:** con el tema sintético de 10 min y 8 tempos de 98 a 172 BPM, `detect_grid` da lo mismo que antes, y los puntajes son idénticos (hay una prueba).
- Medido en local, con un solo hilo y en tiempo de CPU: `detect_grid` baja de 19,8 s a 0,6 s, y `analyze` de **35 s a 18 s** con un tema de 10 min.
- **Tope por trabajo (`TOPE_TRABAJO_S`=420):** no hace falta subirlo, ni partir el análisis, ni bajar la resolución. Aunque Railway vaya 3 veces más lento, un tema de 10 min queda en ~1 min de análisis (~2 min antes de este cambio). La cifra de 140 s que se dio en #22 incluía la compilación inicial de numba y la CPU compartida con otras pruebas. Se confirma con el log de #22 (`OK en N s`) en la primera tanda.

## 7.6.6 (3-oct-2026) — CARGA MASIVA: TIEMPOS A LA VISTA Y SIN REINTENTOS INÚTILES
- **El log dice cuánto tardó cada trabajo:** `OK en 212 s (tema de 412 s)` o `FALLO en 3 s: …`. Sirve para ajustar `TOPE_TRABAJO_S` (420 s) con datos reales. En un Apple M4, `analyze` tarda 140 s con un tema de 10 min. En Railway la CPU suele ir 2 a 3 veces más lenta, así que un tema largo podría acercarse al tope. Hay que medirlo en la primera tanda.
- **Archivo muy grande = `determinista:`** (como en grid_verifier): `worker-result` hoy reintenta todo error hasta 3 veces, aunque el archivo siga pesando lo mismo. Con el prefijo, Funciones puede cerrarlo al primer intento. Mientras tanto se comporta igual: 3 intentos, cada uno rechazado por `content-length`, sin descargar.
- Los demás errores (red, tope de tiempo) se siguen reintentando.
- Pruebas: 3 nuevas en `tests/test_carga_masiva.py` (con el código anterior fallan 2).

## 7.6.5 (3-oct-2026) — ENERGÍA SIN SATURAR, COMO CAMPO APARTE (#248)
- La energía 1-10 (`energy`) se satura: con un groove sintético masterizado de −16,5 a −3,5 LUFS da **8 siempre**. Con los 7 temas del golden set da 7 u 8.
- El análisis manda además **`energy_v2`** (de `analizador_v8.energia_v2`: LUFS estéreo de −20 a −6, agudos absolutos y golpes por segundo). En el mismo groove va de **5 a 7**. **`energy` no cambia**: la v2 no está calibrada y cambiarla a ciegas mueve las curvas de las listas y la radio.
- `worker-result` ignora los campos que no conoce: hasta que Funciones agregue la columna, `energy_v2` solo queda en el log (`energia: 8 (v2: 6)`). Con la columna, se comparan las dos en el catálogo real antes de decidir.
- Se mide en la misma carga a 44,1 kHz que ya se hacía para `loudness_lufs` (ahora en estéreo). `loudness_lufs` da el mismo valor. Costo, medido en local: **+2 s** por tema de 7 min y +300 MB en ese paso, por debajo del pico de `analyze` (4,5 GB con un tema de 10 min): el pico del trabajo no sube.
- El Dockerfile del análisis ahora copia `analizador_v8.py` (antes no estaba en la imagen).
- Pruebas: `tests/test_energia_v2.py` (con el código anterior fallan 2 de 4; la prueba del trabajo completo comprueba que `energy` sale intacta).

## 7.6.4 (2-oct-2026) — TEMPO: LA SEMILLA DE 2/3 (#206, segunda parte)
- Medido con el golden set **sin BPM previo** (7 temas de 122–124 BPM):
  - antes de #10: **0 de 7** correctos (115 · 121,4 · 115 · 125,3 · 121,4 · 131,75 · 164);
  - con #10: **6 de 7** (falla «Right Thing», 123 → 174,33);
  - ahora: **7 de 7** exactos y 6 de 7 con tempo «constante».
- Causa del que quedaba: librosa da **80,75 = 2/3 · 123**. Solo se probaba duplicar (161,5 → 174,33). Ahora `semillas_candidatas` prueba las octavas (×1, ×2, ×½…) y además ×1,5, ×2/3, ×4/3 y ×0,75, siempre entre 90 y 180 BPM. Una semilla que no es octava solo gana si su rejilla puntúa **≥ 10 % más** que la mejor octava: Right Thing +26 % (correcta) y Day 'N' Nite +1,6 % (incorrecta, se descarta).
- Ojo: el umbral sale de estos 7 temas. Se confirma con el reanálisis en producción (#206).

## 7.6.3 (2-oct-2026) — GÉNERO DETECTADO, ETAPA A: ETIQUETAS
- Para la subida sin ordenar (~1.000 temas; cada género alimenta su emisora): el análisis lee la etiqueta de género del **original** (ID3 `TCON`, Vorbis, MP4) con ffmpeg y la lleva al nombre de Beatport que usa la Biblioteca.
- Envía `genre_detected`, `genre_confidence` y `genre_source='etiqueta'` por worker-result. Confianza 0,95 si el género se reconoce; 0,6 si no lo conocemos (se respeta); 0,3 si es demasiado amplio («Electronic», «Dance»). Con menos de 0,7, o sin etiqueta, suma la bandera `genero_por_revisar` a `analysis_flags`.
- La etapa B (clasificador por audio para los temas sin etiqueta) llega con el catálogo etiquetado.
- Pruebas: `tests/test_genero.py`. Con los 7 temas del golden set: 7 de 7 leídos.

## 7.6.2 (2-oct-2026) — LISTO PARA LA CARGA MASIVA (~1.000 temas, 5 réplicas)
- **Tope por trabajo de 7 min** (`TOPE_TRABAJO_S`, por defecto 420) para todo el trabajo: descarga, análisis, CM2, rendición, master y subidas. Antes solo el análisis tenía tope, de 10 min, y `claim_analysis_job` retoma un `processing` a los **8 min**: un tema lento lo podía tomar otra réplica a la vez. El `for update skip locked` del reclamo ya era correcto.
- **`MAX_TRACK_MB`** (por defecto 250): el original se baja por partes a disco (antes `r.content`, entero en RAM) y se rechaza si pasa el tope, por `content-length` o al ir bajando, sin dejar el temporal.
- **grid_verifier 1.1.2:** `no_anchor` deja de ser «determinista». Es una carrera: la verificación se encola al subir, antes del ancla, y otra vez cuando el análisis termina. Ahora se reporta `esperando_analisis:no_anchor`.
- stems_worker 1.23.2: solo la versión de su log de arranque, para que este despliegue también incluya stems (la 7.6.1 se saltó por las watch paths).
- Pruebas: `tests/test_carga_masiva.py`.

## Registros sin URLs firmadas (2-oct-2026) — W6 · stems_worker 1.23.1 · grid_verifier 1.1.1
- Los errores de `requests`/`urllib` traen la URL completa («404 … for url: https://…?token=eyJ…»). Terminaban en los registros de Railway (tracebacks de `stems-worker`, auditoría §2) y en el `error` que se guarda en la base.
- `sin_firma()` tapa `token=`, `signature=`, `X-Amz-Signature=`, `X-Amz-Credential=`, `X-Amz-Security-Token=`, `apikey=` y cualquier JWT (`eyJ….….…`). Se aplica a **todo lo que se imprime** (print, `log` y `traceback.print_exc`, envolviendo stdout y stderr al arrancar) y al `error` que va a `worker-result`, `stems-result`, `loops-result`, `render-result` y `grid-verify-result`.
- El `error` que `render_set` manda a `set-render` lo limpia `_sin_firmas` (7.6, #9).
- Copia idéntica en los tres archivos (cada imagen copia solo el suyo); una prueba exige que no se separen.
- Pruebas: `tests/test_sin_firma.py`.

## 7.6.1 (2-oct-2026) — COPIAS DE ESCUCHA SIN ETIQUETAS (#265, privacidad)
- La copia de escucha (MP3 192k) se genera **sin metadatos** (`-map_metadata -1`, sin ID3v1/ID3v2, sin capítulos) y **sin portada embebida** (`-map 0:a:0`). La radio y el catálogo la sirven a anónimos, y el visitante nunca debe ver el título ni el artista reales.
- El master MP3 320k de descarga (dueño o comprador) conserva sus etiquetas.
- **Reproceso de las copias existentes**, en tandas por la cola `stream-limpiar` (`next` / `result` / `fail`): se baja la copia, se quitan las etiquetas **sin re-codificar** (mismo audio y mismo timeline: rejilla y cues siguen valiendo) y se sube a la **misma ruta**. El worker no escribe en la base. Corre solo con la cola de análisis vacía; si la función todavía no existe (404), se omite.
- Medido con 3 copias reales de producción: traían `TIT2`; después, ninguna etiqueta, con la misma duración (393,64 → 393,64 s) y ~400 bytes menos.
- Pruebas: `tests/test_stream_sin_etiquetas.py`.

## 7.6 (2-oct-2026) — EL SET SIGUE EL PLAN DEL DJ (#143)
- «Convertir en set» manda en `spec.transiciones` el plan de cada par, sacado del mismo planificador que suena en las listas. Antes `render_set` lo ignoraba: estiraba todo el set a un solo tempo y cruzaba 16 compases fijos, sin eco ni corte.
- Ahora sigue el plan con las cifras del navegador:
  - cada tema suena a su tempo; la entrante va a `rate` durante la mezcla y vuelve a su tempo en `release_seg`;
  - ganancias equal-power con `asimetria`; graves con shelf de 120 Hz (swap −12 dB en `graves_swap_en`, o −6 → 0 dB sin swap);
  - eco a tempo (1 beat, realimentación 0,45, cola de 8 beats); corte y encadenado (al final útil si `salida_seg` es null).
- Si `spec.transiciones` no coincide con los temas (orden, cantidad o tipo), usa el método anterior: los sets viejos no cambian.
- Si una mezcla no se puede igualar en tempo (fallan rubberband y atempo), lo avisa en el log y esa transición pasa con eco en el mismo compás: nunca se cruzan dos tempos sin igualar, igual que en el planificador.
- Una descarga que falla deja solo el código HTTP y el host: la URL firmada no llega al log ni a `set_render_jobs.error`.
- El temporal `dm_set_*` se borra siempre (antes nunca se borraba).
- Sigue apagado si `ENABLE_SET_RENDER` no está en Railway.
- Pruebas: `tests/test_set_plan.py` (corte, encadenado, eco, mezcla con temas sintéticos a 124 y 126 BPM: la entrante suena a 124 durante la mezcla).

## 7.5.4 (2-oct-2026) — TEMPO CORRECTO SIN BPM PREVIO
- Un tema que llega sin BPM (sin etiqueta) toma el tempo de `detect_tempo`. La semilla de librosa sale cuantizada (a 11025 Hz y hop 512: 99,4 · 107,7 · 117,5 · 129,2 · 143,6…) y la búsqueda era de ±4 BPM alrededor de ella, así que **121,5–125,1 y 133,2–139,5 BPM no se podían encontrar**. Un tema de 124 salía 125,3; uno de 135, 125,3; uno de 6 min a 124, 128,7.
- En producción se veía como `v7 bpm 121.42 → 121.44 (resid 104.9 ms, variable)`: el tempo pegado al borde de la búsqueda y la rejilla marcada como «variable».
- Ahora la búsqueda gruesa es de ±8 % de la semilla (mínimo ±4 BPM), que cubre el salto entre dos valores vecinos hasta ~180 BPM. Cuesta ~30 % más en ese paso (20 s → 26 s en un tema de 6 min).
- No cambia nada para los temas con BPM previo: el v7 sigue afinando alrededor de ese BPM.
- **Tempo efectivo coherente** (`worker.py`): `bpm_fine` se calcula contra el mismo entero que usa `worker-result`. Sin BPM previo, ese entero es `Math.round(bpm)` de JS, que redondea ,5 hacia arriba; el `round` de Python redondea al par (124,5 → 124 aquí y 125 allá), y el tempo guardado quedaba 1 BPM corrido.
- **Con BPM bloqueado** (etiqueta, manual o `bpm_tag`; es el único caso en que `worker-next` manda semilla), `bpm` sigue siendo la medida propia de `detect_grid`. `worker-result` no toca el entero bloqueado: usa `bpm_precise` para el decimal y compara `bpm` con la etiqueta para la bandera `bpm_etiqueta_difiere`. Un primer intento de este PR mandaba el entero de la semilla y apagaba esa bandera para siempre (lo vio el integrador).
- Pruebas: `tests/test_tempo_sin_semilla.py` (4 de 8 fallan con el código anterior) y `tests/test_tempo_coherente.py` (replica `decidirTempo` y la bandera de `worker-result`).



## Espera creciente en los tres workers (30-sep-2026) — W2 · stems_worker 1.22.4
- Con la cola vacía, la espera arranca en `POLL_INTERVAL_SECONDS`, se duplica en cada vuelta sin trabajo hasta `POLL_MAX_SECONDS` y vuelve al inicio apenas llega un trabajo. Los errores de red también esperan más, con ±20 % para que las réplicas no choquen.
- Topes: análisis y stems 120 s; verificador 300 s (nadie lo espera en pantalla).
- Antes: ~73.000 consultas al día con las colas vacías (3 réplicas cada 5 s, stems 3 consultas cada 15 s, verificador cada 20 s). Ahora ≈4.600 (≈3.200 cuando W1 deje una réplica).
- Costo: tras un rato sin trabajo, un análisis o un render puede tardar hasta 2 min en arrancar.
- Pruebas: `tests/test_espera.py`.

## grid_verifier 1.1.0 (30-sep-2026) — SIN REINTENTOS INÚTILES (W4)
- Antes de bajar el audio revisa el catálogo: sin BPM, sin ancla o con `duration_seconds` < 30 s se reporta al instante, sin descarga.
- La medida se valida con los mismos rangos que `grid-verify-result` (vacía, no finita o fuera de rango): antes llegaba y la plataforma respondía 400 `bad_measurement`.
- `no_bpm`, `no_anchor`, `too_short`, `no_fit` y `bad_measurement` se reportan como `determinista:<código>`. Para cortar los 3 intentos, `finish_grid_verify_job` tiene que respetar ese prefijo (pedido a Funciones en #30).
- Pruebas: `tests/test_grid_verifier.py`, con un tema sintético medido de punta a punta.

## 7.5.3 (30-sep-2026) — CM2 CON CLAVE Y CON EXAMEN (W3)
- CM2 pedía la rendición a `stream-track` **sin `x-worker-secret`**: fallaba en cada tema (404 en el proyecto de Lovable, 401 en el nuevo). Ahora `descargar_rendicion` manda la clave, también en el examen golden.
- Sigue pidiendo el formato por defecto (`aac`, que cae a MP3 si no hay m4a): es el mismo audio que oye el navegador. Si `stream-track` da 5xx (el m4a legado no se migró al Storage nuevo, issue #35 de la plataforma), pide `format=mp3`.
- CM2 solo escribe `first_beat_detected_ms` con `ENABLE_ANCHOR_BACKFILL=true` **y** el examen golden aprobado en ese arranque (antes el examen no bloqueaba nada). Hoy el examen sale NO APROBADO, así que no cambia ninguna rejilla.
- El temporal de la rendición se borra aunque falle el cálculo (`finally`).
- Pruebas: `tests/test_cm2.py`.

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

## 1.23.0 (30-sep-2026) — MOTOR HÍBRIDO DE SEPARACIÓN (APAGADO POR DEFECTO)
- **Nada cambia si no se pide**: sin `STEMS_ENGINE` y sin `model: "hibrido"` en el trabajo, separa Demucs igual que la 1.22.4 (WAV + ffmpeg de la 1.22.3 y espera creciente de W2). El híbrido codifica con la misma cadena de ffmpeg.
- Selector `elegir_motor(job)`: `"hibrido"` solo si el trabajo trae `model == "hibrido"` o si `STEMS_ENGINE=hibrido`. Punto único `separar()`.
- Motor híbrido (`separar_hibrido`), mismas 6 pistas con los mismos nombres:
  - voz: Mel-Band RoFormer de Kimberley Jensen (MIT, `KimberleyJSN/melbandroformer`); instrumental = mezcla − voz;
  - batería y bajo: SCNet-XL de ZFTurbo (MIT, release v1.0.13) sobre el instrumental;
  - piano y guitarra: htdemucs_6s sobre el instrumental;
  - other: lo que queda (la suma de las 6 vuelve a dar la mezcla); `HIBRIDO_OTHER=scnet` usa el other de SCNet − piano − guitarra.
- Código de los modelos (4 archivos de ZFTurbo/Music-Source-Separation-Training fijados por commit) y pesos (~1,1 GB) se bajan la primera vez a `HIBRIDO_DIR`, verificados por sha256. Opcional: `--build-arg PRECARGAR_HIBRIDO=1` los mete en la imagen.
- El resultado lleva `engine` y `engine_version` en `patterns`; si el híbrido falla, separa Demucs y deja `engine_fallback`.
- Nuevo `banco_separacion.py` (no se despliega): A/B demucs vs híbrido con las medidas del worker (calidad por pista, fuga de voz, desvío y deriva del bombo, loops aprobados por `controlar_loop`, notas de bajo, tiempo y RAM) + carpetas de escucha ciega A/B con clave aparte.
- Dependencias nuevas (~3,5 MB): `einops`, `rotary-embedding-torch`, `beartype`, `packaging`.
- Pruebas: `tests/test_selector_motor.py` (por defecto demucs, respaldo, trozos con solape).

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
