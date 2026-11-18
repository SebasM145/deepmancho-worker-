# Historial de versiones · worker.py (análisis)

## 7.6.26 (18-nov-2026) — LA 7.6.25 (#71) CON EL CUE A DONDE EMPIEZA A SONAR (#68, dj-connect #930)
Es la versión que sale en la final del 6-dic. Une dos PR que nunca se desplegaron y que los dos decían 7.6.25: el análisis de la final (#71) y el arreglo del cue A (#68, abierto desde el 16-oct). **Sale una sola versión, la 7.6.26.**

### Qué trae
- **Todo lo de la 7.6.25 de #71, sin cambios** (tono propio, hasta 8 cues claros, salida con cola, sin `mixout_temprano` y el detector nuevo del «1» aparte): ver la sección de abajo.
- **El cue A va al beat de la rejilla donde empieza a sonar** (regla de #68): `posicion_cue_a_ms(ancla, bpm, inicio_ms)` = el último **beat** de la rejilla que no pasa el primer sonido (la regla del dueño en #278, la misma de `posicionCueASec` en el mezclador). Sin dato de sonido, el primer beat ≥ 0. Sin BPM, 0. El primer sonido (`inicio_audible_ms`) usa el criterio del mezclador (más del 2 % del pico), medido sobre las muestras. Lo usan `detect_cues`, `cues_respaldo` y `cuantizar_cues`.
- **B a H siguen la regla de #71:** en el «1» y en frontera de 8 compases contados desde el primer «1». A ya no: puede caer en el 2, el 3 o el 4 del compás, porque ahí empieza el tema (MIK y Rekordbox hacen lo mismo).

### Choques al unir #68 sobre #71
1. **`primer_downbeat_ms` → `posicion_cue_a_ms`.** #68 la reemplazó; #71 la seguía usando en `cues_respaldo` y `cuantizar_cues`. Quedó la de #68 en los tres sitios.
2. **`detect_cues`.** #71 la reescribió entera (hasta 8 cues, salida primero, azar del tema); #68 solo cambiaba dónde va A. Quedó el cuerpo de #71 y A en `posicion_cue_a_ms`.
3. **`cues_respaldo`.** #71 cuenta las frases desde el primer «1»; #68 calcula A con el primer sonido. Quedan las dos: A con el ancla real (misma fase de beat) y las frases desde el primer «1».
4. **El aviso de arranque y este CHANGELOG:** las dos 7.6.25 pasan a una sola 7.6.26. La entrada de #68 queda resumida aquí y la de #71, abajo, entera.

### Medida
Banco del cue A contra Mixed In Key (`bancos/banco_cue_a.py` y el banco de las tres versiones del carril, solo local): 212 temas del XML de Rekordbox de Germán con el A de MIK, el mismo audio, BPM y ancla para las tres versiones. Ni audio ni XML entran al repo.

| | 7.6.24 (main) | 7.6.25 (#71) | **7.6.26** |
|---|---|---|---|
| A a ≤ 50 ms del A de MIK | 49/212 = 23 % (IC 18-29) | 49/212 = 23 % | **174/212 = 82 % (IC 76-87)** |
| Distancia mediana al A de MIK | 440 ms | 440 ms | **41 ms** |
| El mezclador respeta el A al cargar | 59/212 = 28 % | 59/212 = 28 % | **212/212 = 100 %** |
| B a H iguales a los de 7.6.25 | — | — | 212/212 |

- Pareado 7.6.25 → 7.6.26: 130 temas ganan y 5 pierden (McNemar exacto p < 0,001). #71 no tocó el A: el de 7.6.24 y el de 7.6.25 son el mismo en 212/212.
- En 153/212 (72 %) la música empieza más de un beat antes del A de 7.6.24; ahí el A de MIK cae en un beat de nuestra rejilla en el 92 % y en nuestro downbeat en el 1 %.
- **Con la rejilla de Rekordbox** (`worker-result` lleva los cues al «1» de esa rejilla), el A de 7.6.26 solo sirve si `worker-result` no lo corre con los demás (dj-connect #1814): corrido, 44/212 a ≤ 50 ms de MIK y el mezclador lo respeta en 57/212; sin correrlo, 174/212 y 212/212 (hoy, con 7.6.25: 176/212 y 199/212; pareado 8 pierden y 6 ganan, p = 0,79). **Esta versión no se despliega sin ese `worker-result`.**
- **Muestra ciega del 15-nov** (la de #71: subida cruda, sin BPM ni tono de semilla; 56 temas con corrida de 7.6.25 y 7.6.26): BPM, ancla, «1», tono, energía global y de salida y pico, confianza, alarmas, estabilidad y LUFS **idénticos en 56/56**; cues B a H (número, etiqueta, posición y confianza) **idénticos en 56/56**. Solo cambia el A (47/56) y, porque se mide 30 s desde el A, `energy_entry` en 2/56 (±1). A a ≤ 50 ms del de MIK: 10/54 = 19 % (IC 10-31) → **49/54 = 91 % (IC 80-96)**; pareado 40 ganan y 1 pierde, p < 0,001. 7.6.24 tiene el mismo A que 7.6.25 (#71 no lo tocó).

### Pruebas
- `tests/test_930_cue_a.py` (de #68) y `tests/test_1117_cues_y_salida.py` (de #71) en la misma batería, más una prueba de las dos reglas juntas. Sale `test_930_cue_a_downbeat.py` (probaba la regla de 7.6.24).
- Resultado: 858 pasan, 39 saltadas, 2 fallos esperados, 0 fallan (#71: 851 · 39 · 2). `ruff --select E9,F63,F7,F82` limpio.

## 7.6.25 (17-nov-2026, PR #71; no se desplegó sola: sale dentro de la 7.6.26) — TONO PROPIO SIEMPRE, HASTA 8 CUES CLAROS, SALIDA CON COLA Y SIN ALARMA FALSA, Y EL DETECTOR NUEVO DEL «1» LISTO PARA ENCENDER
Sale de la auditoría del 15-nov de dj-connect (`docs/auditorias/2026-11-15/analisis-sin-rekordbox.md`, §6 y §7) y del sí de Germán del 17-nov (T1, D-AN-3, C5 y D-AN-7) y de su pedido de ese mismo día: que el «1» salga bien desde la subida. **El worker sigue mandando el mismo BPM y la misma ancla de la 7.6.24.** La rejilla nueva viaja aparte, y si se usa o no lo decide un interruptor de la plataforma, apagado de fábrica.

### 1. Tono propio siempre (T1)
- `process_job` manda `key_detected` con el tono (Camelot) que midió `detect_key`, **siempre**. `key` no cambia: sigue viajando solo si el tema no traía tono, y `worker-result` nunca pisa una etiqueta ni un dato manual.
- **Por qué:** con etiqueta, nuestro tono se descartaba (`result["key"] = None`) y no había con qué compararlo. La auditoría tuvo que medirlo aparte: 29 de 41 = 71 % exacto donde Rekordbox y MIK coinciden. Ahora queda guardado en cada análisis.

### 2. Hasta 8 hot cues, todos en el «1» y en frontera de 8 compases (C1 y C3)
- **Inicio (A) y Salida (H) siempre.** H es siempre el cue 7 (`MIX-OUT`), haya los del medio que haya: antes, con menos de 8 cues, la salida quedaba numerada 5 o 6 y con la etiqueta «DROP 2» o «VOCALS».
- **Del medio, hasta 6, solo los claros.** Un cambio es claro si cumple las dos:
  1. **se oye:** el nivel de las 4 bandas cambia en promedio 1 dB o más entre los 4 compases de antes y los 4 de después (`CAMBIO_AUDIBLE_DB`; 1 dB es más o menos el menor cambio de nivel que se alcanza a oír);
  2. **le gana al azar del propio tema:** cambia más que la mediana del cambio medido en las **medias frases** del mismo tema, que es donde caería un cue puesto sin oír la canción.
- **Todos en frontera de 8 compases contados desde el primer «1»** (`CUE_FRASE`). Los candidatos ya no van cada 4 compases, y `cuantizar_cues` lleva los cues al compás y a la frase (antes, los del medio iban al beat). El plan B también cuenta sus frases desde el primer «1» y no desde el ancla.

### 3. Salida con cola y sin alarma falsa (C5)
- **Tema de 5 min o más:** H va entre 32 y 48 compases antes del final del audio útil, en la frontera con más bajada **sostenida**.
- **Tema corto:** H va en la última bajada real que deja al menos 16 compases de cola, sin pasar de medio tema. Si no hay bajada, va en la última frontera con esa cola y se dice (`salida_sin_bajada`); si ni eso cabe, `salida_sin_cola`.
- **«Bajada real»** (`BAJADA_REAL_DB` = 1 dB): desde H hasta el final, el nivel medio de las 4 bandas queda al menos 1 dB por debajo de los 8 compases anteriores. Se mide contra **toda la cola**: un break de 8 compases también baja, pero después la música vuelve.
- **Se quitó `mixout_temprano`.** El detector ponía H desde el 75 % del tema y la revisión exigía el 80 %: saltaba en 48 de 72 subidas reales y en 31 de 58 temas de la muestra, y cada una le quitaba 0,2 a `analysis_confidence`.
- **La confianza de H ahora dice algo** (tramos de `cueConfidence.ts` de la app): 1,0 con bajada real; 0,6 en un tema largo sin bajada (la cola alcanza: mezcla completa); 0,5 en un tema corto sin bajada (mezcla corta de 8 compases); 0,3 sin cola (el Auto no la usa y se dibuja punteada).

### Medida de 2 y 3 (antes 7.6.24, después 7.6.25)
Los mismos 58 temas de la muestra ciega del 15-nov (semilla 20261115; 51 de `entrenar` y 7 de `ajuste`; `prueba` sigue sellada), sin BPM ni tono de semilla, como una subida cruda. Las reglas se eligieron mirando 26 temas (la mitad de `entrenar`) y se confirmaron en los otros 32.

| Medida | 7.6.24 | 7.6.25 | Vara de §6 |
|---|---|---|---|
| BPM, ancla y tono que se guardan | — | **idénticos** en los 55 | no cambian |
| Cues en frontera de 8 compases, contra la rejilla de **Rekordbox**, en los temas con el «1» bien (10 temas) | 70/80 = 88 % (IC 78-93) | **77/77 = 100 %** (IC 95-100) | ≥ 85 % (MIK: 88 %) |
| Cues en frontera de 8 contados desde nuestro primer «1» (58 temas; se cumple por construcción, no es mérito) | 419/464 = 90 % | 456/456 = 100 % | — |
| Cues del medio que NO son claros (bajo el azar del tema o < 1 dB) | 6/348 = 2 % | **0/340** | 0 |
| Temas con 8 cues · con menos | 58 · 0 | 54 · 4 (uno con 5, dos con 6, uno con 7) | hasta 8 |
| La salida es el cue 7 (`MIX-OUT`) | 58/58 (todos traían 8) | 58/58, también con menos de 8 | siempre |
| **Salida: cola ≥ 32 compases** en temas de 5 min o más (50) | 38/50 = 76 % (IC 63-86) | **50/50 = 100 %** (IC 93-100); pareado 0 pierden, 12 ganan, p < 0,001 | ≥ 80 % |
| Cola de la salida, los 58 temas (p10 · mediana · p90) | 28 · 32 · 48 | 30 · 38 · 47 | 32 a 48 en los largos |
| **Salida en bajada real** (1 dB sostenido), temas de 5 min o más | 21/50 = 42 % (IC 29-56) | **30/50 = 60 %** (IC 46-72); pareado 4 pierden, 13 ganan, p = 0,049 | ≥ 90 %: **no se cumple** |
| Alarma `mixout_temprano` | 28 de 55 temas | **0** (ya no existe) | quitarla |
| Temas sin ninguna alarma | 26 de 55 | 53 de 55 | — |
| `analysis_confidence` = 1,0 | 26 de 55 | 53 de 55 | — |
| Alarmas nuevas | — | `salida_sin_bajada` en 1 (un tema corto) | decirlo |
| Cues de MIK con uno nuestro a ≤ 2 compases (referencia, no meta) | 304/413 = 74 % (IC 69-78) | 282/413 = 68 % (IC 64-73) | base sin oír: 53 % |
| Salida a ≤ 8 compases de la de MIK (referencia) | 27/53 = 51 % | 15/53 = 28 % | otro criterio (MIK sale en el último drop) |

Las filas «de 55» son la corrida completa del worker (`analyze`, de punta a punta) antes y después; 3 temas de los 58 no terminaron por un fallo de la herramienta de medida (el reparto de procesos se queda esperando, el mismo que anotó la auditoría del 15-nov). Las filas «de 58» y «de 50» salen de las mismas funciones del worker sobre la energía por compás de cada tema: en los 55 con corrida completa dan los mismos cues, tema por tema (55 de 55). En los 32 temas que no se usaron para elegir las reglas: cola ≥ 32 en 20/28 → 28/28 y bajada real en 12/28 → 17/28.

- **Lo que NO se cumple, dicho de frente.** La vara de §6 pide «bajada real ≥ 90 %». Queda en 30 de 50 = 60 % de los temas largos (antes, 42 %). No es el detector: solo en esa proporción de los temas largos hay una bajada sostenida de 1 dB dentro de la ventana de 32 a 48 compases; abriendo la ventana a 24-48 serían 39 de 50, y a 16-80, 44 de 50. Germán pidió la ventana de 32 a 48, así que H va ahí y, cuando no hay bajada, lo dice su confianza (0,6). Mover la ventana es decisión suya.
- **«Menos de 8 cuando la canción no da» casi no aparece en esta música:** 54 de 58 temas siguen con 8 cues. Con el detector de hoy solo 6 de 348 cues del medio quedaban por debajo de la vara. La regla muerde en temas planos y cortos, que en la colección de Germán casi no hay (8 de 58 duran menos de 5 min) y en las subidas reales sí (28 de 72 duran menos de 2,5 min). **Eso no se pudo medir:** no hay audio de esas subidas fuera de producción.
- **Parecerse a MIK baja un poco y ya no es la meta (C9):** cues de MIK con uno nuestro a ≤ 2 compases, de 74 % a 68 %. La base sin oír (un cue cada 16 compases) da 53 %. Es el precio de sacar la salida del último drop (donde la pone MIK) y llevarla a donde deja cola.
- **Límites:** 58 temas dan unos ±12 puntos. Es música de un solo dueño, house y techno de 105 a 150 en versión extendida. `ajuste` ya estaba gastado antes de esto. «En el “1”» sigue dependiendo de la rejilla, que no cambia: con el «1» de hoy mal puesto en 4 de cada 5 subidas crudas, los cues siguen heredando ese error hasta que el detector nuevo pase de sombra a real.

### 4. El detector nuevo del «1» (D-AN-7, R3 y R2): el worker lo calcula y la plataforma decide
- `rejilla_v2.py`: Beat This! `small0` (CPJKU, ISMIR 2024, MIT) en CPU + rejilla global + sesgo medido + árbitro. Es el **mismo cálculo** del banco de dj-connect (`tools/analizador`), llevado a Python: con las predicciones guardadas de la biblioteca de entrenamiento da **904 de 904 bloques idénticos** (los 7 campos del banco), y `tests/test_rejilla_v2.py` lo compara con 8 casos sintéticos calculados allá. Y con audio real, de punta a punta (ffmpeg, modelo, rejilla, árbitro): **55 de 55 bloques idénticos** a los del banco en los temas de la muestra del 15-nov que tienen bloque guardado.
- **El worker no decide nada.** Manda un bloque `v2` **aparte** y no cambia `bpm`, `bpm_fine`, el ancla ni los cues que ya mandaba. El bloque trae `bpm`, `bpm_fino`, `bpm_redondo` (R2: el entero si cae a ≤ 0,1), `ancla_ms`, `regla` («voto» o «primer_beat»), `acuerdo`, `confianza`, `intro_s`, `version` (hash de modelo + árbitro, el mismo del banco), las medidas con las que decide el árbitro y el tiempo y la memoria que gastó.
- **D-AN-10 (Germán, 17-nov: «sí, encendamos y conectemos todo»):** en la versión final el detector nuevo se enciende de verdad para los temas sin candado, sin esperar el XML de otros DJ. Encenderlo es un paso del guion de publicación (dos filas en la base), no de este PR.
- **Dos interruptores, los dos en la plataforma y los dos apagados de fábrica** (tabla `analisis_config` de dj-connect; se cambian con una fila, sin desplegar ni tocar Railway):
  1. `sombra_v2`: `worker-next` le pide al worker el bloque (`sombra_v2: true` en la respuesta). `worker-result` lo guarda en `music_tracks.analisis_v2` y nada más.
  2. `rejilla_v2_real`: además, `worker-result` escribe el ancla y el tempo del bloque con `grid_source = 'analizador_v2'`, **solo en temas sin candado** (nunca rejilla `rekordbox`, `manual`, `audio` o heredada; nunca BPM de etiqueta o manual; nunca si su tempo queda a más de 0,5 BPM del que midió el detector de hoy), y lleva los cues nuestros al «1» nuevo.
- El worker anuncia la capacidad `sombra-v2-1` solo si tiene instalados el modelo y sus pesos. `SOMBRA_V2=true` en Railway lo fuerza siempre (para probar); `SOMBRA_V2=false` es el freno de mano: ni se anuncia ni se calcula, diga lo que diga la plataforma.
- **No puede tumbar un análisis.** Corre al final (después de la copia de escucha y del máster), en un **proceso nuevo** (`python rejilla_v2.py tema`), con tope (`SOMBRA_V2_TOPE_S`, 150 s) y solo con el tiempo que sobre del tope del trabajo. Si falla, se cae o tarda de más, el resultado se entrega igual, el bloque dice `error` (`fallo`, `caido`, `tope`, `sin_tiempo`, `sin_rejilla`) y la rejilla queda en manos del detector de hoy.
- **Por qué un proceso nuevo y no un hijo por fork como el análisis:** en la medida local, cargar torch en un proceso que ya había corrido librosa y numba se quedó trabado en un futex (2 de 2 intentos). Un intérprete nuevo no hereda nada; cuesta unos segundos más por tema, y están en la tabla de abajo.
- **Contrato:** `worker-result` y `worker-next` de antes ignoran lo nuevo, y un worker de antes no lo manda ni lo anuncia: función y contenedor se despliegan en cualquier orden.
- **Imagen:** `requirements-v2.txt` (torch de CPU 2.15, torchaudio, beat-this 1.1.0) y los pesos `small0` (8 MB), que se bajan al construir (3 intentos, 60 s de espera cada uno) y se comprueban por sha256. **Si no bajan o no son los esperados, la imagen no se construye** y Railway se queda con la anterior: un despliegue sin pesos no puede quedar en silencio. No van dentro del repo porque no hemos confirmado la licencia de los pesos para redistribuirlos; si el servidor de la universidad falla seguido, se suben a un release propio con el mismo sha256. En marcha el worker no baja nada. **La imagen pasa de 1,52 a 2,77 GB.**

### Humo: tiempo y memoria en un contenedor como una réplica de Railway
`bancos/humo_sombra.py`, en la imagen del `Dockerfile` (`python:3.11-slim`), con **`--cpus 2 --memory 10g`** (lo que tiene cada una de las 3 réplicas del servicio) y 12 temas **sintéticos** (de 2 a 10 min; tres de 8 a 10 min, uno sin beats, uno en 3/4, uno a 95 y uno a 150). Cada tema hace lo que hace un trabajo de verdad: el análisis, la sonoridad, la copia de escucha y, al final, el modelo, con el reloj del trabajo corriendo desde el principio. El padre corre antes su examen CM2, como en producción. El servidor tenía otros trabajos corriendo: los tiempos son más bien pesimistas. **Mide operación, no calidad** (pocos temas, y sintéticos).

| Tema sintético | Dura | Análisis de hoy | Copia de escucha | + detector nuevo (2 hilos) | Trabajo entero (tope 420 s) | Memoria del proceso del modelo | ¿Quedó con bloque? |
|---|---|---|---|---|---|---|---|
| House 124 | 6 min | 71,7 s | 5,9 s | 24,2 s | 101,8 s | 511 MB | sí |
| Techno 130 | 7 min | 63,6 s | 5,9 s | 23,9 s | 93,4 s | 558 MB | sí |
| Deep 120 | 5 min | 44,0 s | 4,8 s | 19,3 s | 68,0 s | 466 MB | sí |
| Tech 126, intro de 15 s | 6 min | 54,7 s | 5,8 s | 23,9 s | 84,4 s | 509 MB | sí |
| Rápido 150 | 4 min | 35,6 s | 3,4 s | 17,8 s | 56,8 s | 420 MB | sí |
| Corto 128 | 2 min | 14,3 s | 1,7 s | 9,7 s | 25,7 s | 377 MB | sí |
| Lento 95 | 3 min | 23,2 s | 3,6 s | 14,8 s | 41,6 s | 373 MB | sí |
| Largo 125 | 10 min | 75,8 s | 8,8 s | 40,0 s | 124,6 s | 665 MB | sí |
| Largo 128 | 9 min | 79,6 s | 9,0 s | 37,8 s | 126,4 s | 616 MB | sí |
| Largo 122 | 8 min | 62,8 s | 8,0 s | 34,6 s | 105,3 s | 599 MB | sí |
| En 3/4, 120 | 4 min | 36,2 s | 3,7 s | 19,6 s | 59,5 s | 418 MB | sí |
| Sin beats | 4 min | 28,2 s | 4,2 s | 21,2 s | 53,6 s | 422 MB | sí |
| **Los 12** | 68 min | 589,7 s | 64,8 s | **286,8 s** (+49 %) | máx. 126,4 s | máx. **665 MB** | **12 de 12** |

| Lo que Germán necesita para decidir | Cifra (2 vCPU y 10 GB, como una réplica de Railway) |
|---|---|
| **Segundos por tema** que suma el detector nuevo | mediana **22,6 s**; el de 10 min, **40,0 s**; unos 4,3 s por minuto de audio |
| Temas largos (8 a 10 min) que se quedan sin bloque (`sin_tiempo` o `tope`) | **0 de 3**. El trabajo entero más largo usó 126,4 s de los 420 del tope |
| … con música real (58 temas de la muestra, fuera del contenedor, 3 a la vez y el servidor cargado) | mediana 32,8 s; el peor, 98,7 s; 0 sin bloque |
| **Memoria máxima** del proceso del modelo | **665 MB** (tema de 10 min); 680 MB con música real |
| Pico del contenedor entero | 2,3 a 2,4 GB de los 10: lo pone el análisis de hoy, no el modelo |
| **Tamaño de la imagen** | de **1,52 GB a 2,77 GB** (+1,25 GB: torch de CPU) |
| Con 1 hilo en vez de 2 | 6 min → 33,4 s, 10 min → 54,6 s, 9 min → 53,1 s, 8 min → 47,8 s |
| Fallos del modelo | 0 de 16 corridas en el contenedor y 0 de 58 con música real |

- **Cabe.** El detector nuevo suma unos 4,3 s por minuto de audio (la mitad más de lo que ya tarda el análisis) y su proceso no pasa de 0,7 GB. El pico del contenedor no sube, porque lo pone el análisis de hoy (2,3 GB aquí; 4,5 GB medido antes con un tema real de 10 min) y el modelo corre después, cuando ese proceso ya devolvió su memoria.
- **Ningún tema largo se quedó sin bloque.** El trabajo más largo usó 126 s de los 420 del tope. El tope del modelo (`SOMBRA_V2_TOPE_S`, 150 s) es casi 4 veces lo que tardó el tema de 10 min (40 s) y vez y media el peor tema real con el servidor cargado (98,7 s). **No hace falta cambiarlo.** Si en Railway los primeros 10 temas muestran tiempos cerca de 100 s, se sube con la variable, sin desplegar código.
- **Costo** (precios de lista de Railway: US$ 20 por vCPU al mes y US$ 10 por GB al mes): unos 23 s de 2 vCPU y 0,5 GB por tema ≈ **US$ 0,0004 por tema**, unos 40 centavos por cada 1.000 temas.
- **Una medida anterior, con 4 CPU y 8 GB** (antes de saber lo que tiene Railway): mediana 16,6 s y 31,5 s el de 10 min. Queda solo como referencia.
- **No se midió en Railway de verdad:** eso es encender el interruptor allá, y por eso el guion lo hace en dos pasos (primero solo la sombra, mirar 10 temas, después la rejilla). Cada réplica tiene sus propios 2 vCPU, así que 3 réplicas a la vez no deberían cambiar estas cifras, pero no se probó.

### Para desplegar (lo hace el integrador, con el sí del dueño)
1. Aplicar en dj-connect la migración `20261117120000_tono_propio_y_sombra_v2.sql` (`scripts/migraciones.sh aplicar`) y desplegar `worker-result` y `worker-next`. El orden no importa.
2. Mezclar este PR a `main`: Railway redespliega solo. Con eso ya salen el tono propio, los cues y la salida nuevos. **El detector nuevo sigue apagado** (los dos interruptores nacen en `false`).
3. Sumar `rejilla_v2.py`, `requirements-v2.txt` y `parecido.py` a las rutas vigiladas del servicio en Railway (es configuración de Railway: va en el guion de publicación, con el sí de Germán).
4. **Primero solo la sombra:** `UPDATE public.analisis_config SET sombra_v2 = true WHERE id;`. Mirar en los registros de Railway la línea `[sombra v2] … en N s, pico M MB` de los primeros 10 temas: si N se acerca a 100 s o aparecen `tope` o `sin_tiempo`, parar ahí.
5. **Después, la rejilla** (D-AN-10): `UPDATE public.analisis_config SET rejilla_v2_real = true WHERE id;`. Apagar: lo mismo con `false`; no hace falta desplegar.
6. Volver a analizar las subidas crudas que ya están (48 temas sin candado al 17-nov, contados en solo lectura; los espejos reciben la rejilla de su original): `supabase/pruebas/1117_reanalizar_crudas_ensayo.sql` (solo cuenta) y `1117_reanalizar_crudas.sql` en dj-connect. A unos 90 s por tema, unos 25 minutos con las 3 réplicas.
- **Los temas ya analizados no cambian solos.** Los cues nuevos, el tono propio y la rejilla nueva llegan al analizar o reanalizar; los temas con cues importados o manuales no se tocan (lo decide `worker-result`).

### Pruebas
- `tests/test_1117_cues_y_salida.py` (18), `tests/test_1117_tono_propio.py` (3), `tests/test_1117_sombra_v2.py` (18) y `tests/test_rejilla_v2.py` (22). Ajustadas: `test_860_cues_mik.py` (la salida ya no se busca desde el 75 %) y `test_worker_puras.py` (sin `mixout_temprano`).
- Batería completa: 851 en verde, 39 saltadas y 2 fallos esperados (las mismas saltadas y esperadas de antes). `ruff` limpio. Ninguna prueba necesita torch.
- En dj-connect: `worker-result/sombra_test.ts`, `worker-result/contrato_test.ts` (con un payload grabado de este worker) y `worker-next/index_test.ts`.

## 7.6.24 (11-oct-2026) — EL CUE A VA AL PRIMER DOWNBEAT DE LA REJILLA, NO AL SEGUNDO 0 (dj-connect #930)
- **Qué pasó:** en producción, el cue A (MIX-IN) estaba en 0 ms en los 1.128 temas `structure_v1`. Pero su primer golpe (`first_beat_offset_ms`) solo estaba en ≤ 30 ms en 90; en 255 caía entre 30 y 500 ms, y en 783 después de 0,5 s (mediana 729 ms). **En 1.038 temas, saltar a A caía fuera del compás.** Germán: «el primer hot cue debe ir donde inicia la canción, como dice la metodología».
- **Causa:** la regla 1 de #860 («A va SIEMPRE al segundo 0 del archivo, punto de carga de MIK»), repetida en tres sitios: `detect_cues`, `cues_respaldo` y `cuantizar_cues`. `detect_cues` ya calculaba el primer downbeat (`origen_ms`, el compás 0 de sus frases), pero no lo usaba para A.
- **Arreglo:** `primer_downbeat_ms(ancla, bpm)` = ancla − k compases, el más temprano ≥ 0. Lo usan los tres sitios, con la rejilla definitiva (ancla de ataque + BPM fino). Sin BPM (plan C, `cues_por_tiempo`), A sigue en 0 porque no hay compás.
- **Sin cambio:** B–H no se mueven. Las frases se siguen contando desde el inicio del archivo, en la fase del primer beat (#860). Los cues `mixedinkey` y `manual` siguen siendo intocables: eso lo decide `worker-result`, no el worker.
- **Banco MIK:** no se corrió, porque no está el audio en este equipo. La regla cambia por decisión del dueño, no por la medida contra MIK, que ponía A en 0 en 34 de los 50.
- Pruebas: `tests/test_930_cue_a_downbeat.py` (13). Con 7.6.23 fallan las 4 de comportamiento. `test_cues_respaldo.py` ahora espera A en el ancla.

## 7.6.23 (10-oct-2026) — TEMPO SIN ETIQUETA: UNA PROPORCIÓN DE AFUERA YA NO LE GANA A 124 (dj-connect #885)
- **Qué pasó:** en 48 temas con el BPM bloqueado (Mixed In Key o a mano), el `bpm_detected` guardado estaba a 3/4, 5/4, 4/3 o 3/2 del tempo real. Lo guardado es de versiones viejas: con 7.6.20, 34 ya salen bien. Quedaban 14.
- **Medido con su audio** (el stream MP3 público, solo lectura, borrado al terminar) y con el bombo como árbitro:
  - **8 son del detector:** 186 por 124, 184,5 por 123 (3:2), 151,25 y 150 por 121 y 120 (5:4) y 91,5 por 122.
  - **6 son la etiqueta mal puesta:** 90–97 en temas de house, más uno con 123 que es 120. La rejilla del detector junta el 71–94 % de los bombos; la de la etiqueta, el 9–25 %. No es del worker.
- **Causa:** en `0d474d0a` (124), el bajo hace semicorcheas (0, ¼ y ¾ de cada tiempo) dentro de la banda del bombo y con la misma fuerza. El desempate por el bombo de #618 elegía 186 (57 % contra 32 %), aunque la rejilla de ataques prefería 124 (0,466 contra 0,362). En `1289fd09` (123) lo sacaba la regla de #206: la octava explicada por la media rejilla, con el 10 %. En `d91a7c9d` (122), la octava quedaba en 91,5 y 122 ni siquiera era candidata.
- **Arreglo (`grid_detect.py`), con el rango de baile = el de `bpm_fuera_de_rango` (99,5–150,5):**
  1. `saca_del_rango`: una proporción de afuera no compite contra una octava de adentro, ni por el bombo ni por la puntuación. Al revés sí: 160 → 128 de #206 sigue igual.
  2. `volver_al_rango_por_bombo`: si el tempo queda **por debajo** del rango, se prueban sus proporciones de adentro y decide el bombo, con su 25 %. Por encima no se toca.
  - Con semilla (BPM conocido) no cambia nada.

| | 7.6.20 | 7.6.23 |
|---|---|---|
| Los 48: en proporción 1 con lo guardado | 34 | **41** |
| Los 48: correctos según el bombo | 40 | **47** |
| Control: 60 temas al azar que ya salían bien | 60 | 60, ninguno cambia |
| Los 3 del catálogo con etiqueta > 138 (140, «160», «155») | 140 · 128 · 124 | igual |

- **Queda:** `f9a2a9c4` (120) sale 150. Los dos están dentro del rango y el bombo no los separa (48 % contra 41 %).
- **Cambia una decisión de 7.6.19:** sin etiqueta, un 160 de verdad con una percusión cada ¾ o 1¼ tiempos ahora sale 106,5 o 128. En el catálogo no hay ni uno: los 2 con etiqueta de 155 y 160 son 124 y 128 por el bombo, 57 % contra 25 % y 64 % contra 37 %. Con su etiqueta, el tempo que se usa (`bpm_precise`) sigue en 160. Las dos pruebas de control de `test_206_cuatro_tercios.py` ahora pasan la etiqueta.
- Pruebas: `tests/test_885_rango_de_baile.py` (10) usa los puntajes medidos en el audio real. Con 7.6.20 fallan 9. Batería completa: 559 en verde.

## 7.6.22 (10-oct-2026) — UN TEMA REANALIZADO Y LIMPIO BORRA SUS ALARMAS VIEJAS (#206)
- **Qué pasó:** después del reanálisis del 9-oct, `02589893` y `085906cd` quedaron bien (128,071 y 129,993, «constante», confianza 1), pero `analysis_flags` conservó las alarmas del análisis viejo: `bpm_fuera_de_rango:160.007`, `bpm_fuera_de_rango:162.502` y `tempo_variable`. Cualquier pantalla que lea las alarmas mostraba un aviso falso.
- **Causa (worker, no `worker-result`):** el worker solo mandaba `analysis_flags` si la revisión encontraba algún problema. `worker-result` reemplaza las alarmas cuando llega el campo y, si no llega, no toca la columna. Un tema limpio no mandaba nada y se quedaba con las de antes.
- **Arreglo:** cuando la revisión automática corre, `analysis_flags` va siempre, vacía si no hay problemas. `worker-result` ya guarda la lista vacía (más su propia bandera `bpm_etiqueta_difiere`, si aplica). No hace falta tocar funciones ni SQL.
- **Sin cambio:** si la revisión no llega a correr (falla el análisis v7), el campo no va y las alarmas guardadas se conservan, como antes. Las banderas que suma `process_job` (`analisis_parcial`, `bpm_etiqueta_octava`, `genero_por_revisar`) se siguen sumando a la lista.
- **Para cerrar #206:** después de desplegar, reanalizar en silencio los 2 temas (`origen='reanalisis'`); deben quedar con `analysis_flags = []`.
- **Mezclar después de #50 (7.6.21):** los dos tocan la línea de arranque y el inicio de este historial.
- Pruebas: `tests/test_alarmas_reanalisis.py` (2). Con 7.6.20 falla la del tema limpio.

## 7.6.21 (9-oct-2026) — HOT CUES Y ENERGÍA CALIBRADOS CONTRA MIXED IN KEY (#860)
- **Banco:** los 50 temas de «Toque Tech House Piso 3» de Germán, con los 8 cues y la energía de Mixed In Key (dj-connect `docs/hot-cues/referencia-mik-piso3-2026-10-09.json`). Se corre con `bancos/banco_mik.py` (el mismo camino de `analyze`) y se mide con `scripts/medir/cues-mik.mjs` en dj-connect. La corrida local con 7.6.20 da exactamente la línea base de producción (237 de 397, +1,78): el banco es fiel.

| Medida | 7.6.20 | 7.6.21 | Meta |
|---|---|---|---|
| Cues de MIK con uno nuestro a ≤ 2 compases | 237 de 397 (60 %) | **300 de 397 (76 %)** | ≥ 80 % |
| Cues nuestros sin pareja en MIK | 163 de 400 (41 %) | 100 de 400 (25 %) | |
| Energía del tema (nuestra − MIK) | +1,78 (absoluta 1,78) | **−0,04** (absoluta 0,32) | ≤ 0,5 |
| Temas con la energía exacta de MIK | 2 de 50 | 34 de 50 | |

- **Cues (`detect_cues` v7.7):**
  1. Las frases se cuentan desde el **inicio del archivo** (en la fase del primer beat), no desde el ancla. Si el bombo entra tarde (ancla en el compás 4), todos los cues quedaban a 4 compases de los de MIK.
  2. El cambio se mide con **4 compases** antes y después (con 8 se borraban los cambios cortos; AUC 0,89 contra 0,82). Los candidatos van cada 4 compases y el borde de 8 vale un 20 % más, como lo prefiere MIK.
  3. La salida (H) se elige primero, desde el 75 % del audio útil y con 24 compases de cola. Queda en la mediana 0,80 del tema (MIK: 0,79) y nunca antes del 75 %. Después van los 6 cambios más fuertes antes de H, a una frase como mínimo.
- **Energía (`compute_energy`):** `1 + 7·e01` en lugar de `1 + 9·e01`. El tech house salía 8 donde MIK dice 6. El silencio sigue en 1 y lo más fuerte baja de 10 a 8.
- **Sin sobreajuste:** cada regla se eligió mirando el banco partido en dos (temas pares e impares) y da lo mismo en las dos mitades: cues 148/199 y 152/198; energía −0,08 y 0,00.
- **Por qué no llega al 80 %:** con este tipo de señal (energía por bandas), ninguna variante pasó de 302. Eso incluye 22 kHz con 6 bandas, una regresión logística con validación cruzada y una programación dinámica con la distribución de saltos de MIK. Si pudiéramos poner 9 cues llegaríamos a 318 (80 %): el límite es el orden entre el 7.º y el 9.º cambio, no la posición. Queda como siguiente paso.
- **No se tocó:** la energía de cada cue (+0,52 sobre MIK; con base 3,0 bajaría a +0,12). Esa escala se calibró con 7.493 cues de MIK de todo el catálogo, y moverla por 50 temas de un solo estilo sería sobreajustar.
- **Efecto visible al reanalizar:** `energy` baja unos 2 puntos en todo el catálogo (en el buscador, el tech house pasa de «alta» ≥ 8 a «media»), y los cues se mueven. Los temas con `cue_source = 'mixedinkey'` no se tocan (#859).
- **Pruebas:** `tests/test_860_cues_mik.py`, con 4 casos de audio sintético. Con 7.6.20 fallan 2: la fase con intro sin bombo y la escala de la energía.

## 7.6.20 (9-oct-2026) — energy_v2 EN LA ESCALA DEL CATÁLOGO REAL (#248). `energy` NO CAMBIA
- **Qué cambia:** solo `energy_v2`, la candidata que se guarda aparte. La app no la lee: nada cambia en pantalla, en las listas ni en la radio.
- **Por qué:** medido en 40 temas al azar del catálogo del dueño (9-oct), la v2 seguía apretada, con el 70 % en 7, por escala y no por oído:
  - golpes por segundo: tope en 6/s con una mediana real de 6,05, así que **el 52 % de los temas quedaba topado**;
  - agudos: se dividía por 0,25, pero la fracción real va de 0,035 a 0,10;
  - volumen: −20…−6 LUFS, y el catálogo va de −10,7 a −7,3.
- **Arreglo (`analizador_v8.py`):** los mismos tres componentes y los mismos pesos, en la escala real (`V2_LUFS` −13…−6, `V2_AGUDOS` 0,02…0,12, `V2_GOLPES` 3…8/s).

| | Valores que usa | El más común |
|---|---|---|
| `energy` (sin cambio) | 2 | 82 % en 8 |
| `energy_v2` antes | 5 | 70 % en 7 |
| `energy_v2` 7.6.20 | **6** | **30 %** |

- **Es la misma medida, solo bien escalada:** Spearman 0,96 contra la v2 anterior. Moviendo los límites (−14…−6, −12…−6 o −13…−5 LUFS; golpes 2…8, 3…9 o 4…8), el más común queda entre 30 y 38 %.
- **Falta:** H-5 (15 temas con la nota de Germán) para decidir si `energy` pasa a esta escala.
- **Mientras tanto,** `energy_v2` queda con dos escalas en la base: la vieja en lo ya analizado y esta en lo nuevo. Se iguala al reanalizar. Nadie la lee.
- Pruebas: `tests/test_v8_mezcla.py` suma 2, con las medidas reales de los 40 temas (solo números). Con 7.6.19 fallan las dos.

## 7.6.19 (9-oct-2026) — TEMPO SIN ETIQUETA: EL BOMBO DECIDE TAMBIÉN LAS PAREJAS 4:3 Y 5:4 (#206)
- **Qué pasó:** de los 21 temas sin BPM en la etiqueta analizados desde el 5-oct, 2 salieron raros: `02589893` a 160,007 y `085906cd` a 162,502 (162,5 «variable»), los dos «Minimal / Deep Tech».
- **Medido con su audio real (9-oct):** son **128** y **130**. 160/128 = 162,5/130 = **5/4**, no 4/3 como se sospechaba. La autocorrelación del bombo y la de la banda completa dan su pico en 128,00 y 130,00. Con las funciones del detector, la rejilla de 128 junta el 68,7 % de los bombos (la de 160, el 29,4 %), y la de 130 el 46,5 % (la de 162,5, el 24,3 %).
- **Causa:** a 11025 Hz librosa da 86,13. Su doble (172,3) queda como octava y se ajusta a 159,98/162,48, y el tempo real (129,2 = ×1,5) entra como proporción. En `085906cd` la proporción hasta puntuaba más (0,242 contra 0,228), pero no llegaba al 10 % de ventaja. Ninguna regla de desempate miraba las parejas 5:4.
- **Arreglo (`grid_detect.py`):** el desempate por el bombo de #618 (antes solo 3:2) ahora decide también las parejas **4:3** (`en_proporcion_4_3`, una percusión cada 3 semicorcheas, reproducida con audio sintético) y **5:4** (`en_proporcion_5_4`, una percusión cada 0,8 tiempos). Si no hay bombo o no separa las dos rejillas por un 25 %, decide la regla de antes.

| Tema real | 7.6.18 (producción) | solo el arreglo 4:3 | 7.6.19 |
|---|---|---|---|
| `02589893` | 160,007 · constante · ⚠ fuera de rango | 160,007 | **128,071 · constante** · confianza 1,0 |
| `085906cd` | 162,502 · **variable** · ancla rechazada (88,6 ms) | 162,502 | **129,993 · constante** · ancla con residuo 1,1 ms |

- **Un tema de 160 de verdad se queda en 160**, tanto con la percusión de 3 semicorcheas como con una cada 1,25 tiempos (antes del arreglo 5:4, ese salía 128).
- **Los temas ya analizados no cambian solos:** se corrigen al reanalizarlos.
- Pruebas: `tests/test_206_cuatro_tercios.py`, 12 pruebas. Con 7.6.18 fallan 5 de las 4:3; con solo el arreglo 4:3 fallan las 6 de 5:4.

## 7.6.18 (6-oct-2026) — PARECIDO v0 ENTRE UNA TOMA Y SU TEMA SEMILLA (pedido del Estudio)
- **Para qué:** medir cuánto se parece una toma generada («Que suene como un tema mío») al tema del DJ que se usó de semilla. Sirve para el experimento de semillas del Estudio, antes del 17-oct. No usa un modelo de embeddings: es la versión 0.
- **Huella de rasgos:** cada análisis agrega `rasgos` al resultado. Es un resumen liviano del tema, medido sobre el audio ya cargado (sin descargas extra):
  - MFCC 1–19, media y desvío (el 0 queda fuera: mide el volumen);
  - contraste espectral y tercios de octava;
  - curva de energía por compás (32 puntos);
  - BPM y tonalidad.

  La plataforma la guarda (`track_rasgos`) para armar la línea base sin bajar audio.
- **Puntaje bruto (0–100):** 15·BPM + 15·tonalidad + 25·energía + 45·timbre. Las fórmulas están en `parecido.py`.
  - Si a la semilla le falta un dato, su peso se reparte entre los demás.
  - Si la semilla lo tiene y a la toma le falta, cuenta como diferencia.
- **Línea base:** el mismo bruto contra temas del mismo DJ que manda `worker-next`. parecido = clamp(100·(S − media)/(100 − media), 0, 100). Se guardan el bruto, la base y los componentes, para recalibrar.
- **Cuándo se mide:** `process_job` mide solo si `worker-next` manda un pedido `parecido`. El worker anuncia la cabecera `x-worker-capacidades: parecido-1`, así que la función y el contenedor se despliegan en cualquier orden. Si la semilla no tiene huella vigente, el worker la baja y la mide una vez (va en `semilla_rasgos`). Si algo falla, el análisis sigue igual.
- **Banco de calibración:** con la cola vacía, el worker pide tomas a `parecido-next` (que no son music_tracks). Si la función no existe (404), deja de preguntar hasta reiniciar.
- Pruebas: `tests/test_parecido.py`, con audio sintético de resultado conocido (igual 100, casi igual ≥ 75, ambiental sin pulso ≤ 10) y comprobación por mutación de cada regla.

## 7.6.17 (6-oct-2026) — EL ANÁLISIS CORRE EN UN PROCESO HIJO: SI MUERE, LA RÉPLICA SIGUE
- **Qué pasó:** «Touched The Sky» (410 s, MP3 320k) mató la réplica en sus 3 intentos (14:33, 14:41 y 14:50 UTC del 6-oct). El log llegaba hasta `v7 bpm 124.0 → 124.001` y la réplica se reiniciaba sin traceback ni SIGTERM. El tema quedó en «se detuvo 3 veces» tras ~25 min.
- **Lo que se midió:** en local, `analyze` con ese archivo termina en 11 s y pica en 2,5 GB. En el contenedor de Railway (CI, `banco_memoria/`), temas sintéticos de 7 min pican en ~1,6 GB y no crecen de un trabajo al otro. La métrica de Railway (suma de réplicas) llegó a 66,6 GB. **La causa exacta no está confirmada:** memoria del contenedor o un fallo de una librería en C con ese archivo.
- **Arreglo, sirva cual sea la causa:** `analyze`, `medir_sonoridad` y el ancla de CM2 corren en un proceso hijo (`en_proceso_aparte`, fork). Si el hijo muere:
  - la réplica sigue;
  - el log dice con qué señal murió («SIGKILL: casi siempre falta de memoria» o «SIGSEGV: falló una librería en C»);
  - el tema vuelve a la cola al instante con un mensaje claro, en vez de esperar 8 min.

  Además, cada trabajo devuelve toda su memoria al terminar.
- Las excepciones del worker viajan desde el hijo con su clase (`AudioMudo`, `ArchivoIlegible`…). El tope por trabajo y SIGTERM siguen en el padre y cortan al hijo.
- `AISLAR_ANALISIS=false` lo apaga. En macOS viene apagado: allí, fork después de los hilos del sistema rompe al hijo; Railway es Linux.
- Pruebas: `tests/test_aislamiento.py`. En Linux (CI), el análisis real en el hijo da lo mismo que en el padre, con el padre ya «caliente».

## 7.6.16 (6-oct-2026) — MASTER SIN PÉRDIDA EN FLAC (decisión de Germán)
- **Antes (desde la 7.5.1, 30-sep):** un WAV/AIFF/FLAC subido en modo masivo (`needs_master_conversion`) se convertía a **MP3 320k**. `worker-result` cambiaba `audio_asset_path` y borraba el original: el audio sin pérdida se perdía. En la subida normal, la conversión a MP3 la hacía el navegador.
- **Ahora:**
  - un original **PCM entero de 16 o 24 bits** (WAV/AIFF) se guarda en **FLAC** (`-compression_level 8`), con el mismo sample rate, la misma profundidad de bits, los metadatos y la portada (si la portada no entra en FLAC, va sin ella);
  - el FLAC se **verifica muestra por muestra** (`mismo_audio`: sample rate, canales, largo y cada muestra, por bloques) **antes** de reportarlo. Si no es idéntico, no se reemplaza nada y queda el original.
- **Tal cual, sin re-codificar:** MP3, AAC/M4A, FLAC y WAV en coma flotante o de 32 bits enteros (FLAC no los guarda sin pérdida).
- **Nunca más un master MP3:** si la plataforma todavía pide `….mp3` (`worker-next` viejo), el worker no convierte y queda el original.
- Va a `worker-result`: `master_path`, `master_bytes` y `master_mime` (`audio/flac`), para que la cuota (`file_size_bytes`) y el `mime_type` reflejen el FLAC.
- Pruebas: `tests/test_master_flac.py` (WAV 16/24 bits a 44,1/48/96 kHz y AIFF 16/24 → FLAC idéntico; portada y metadatos; MP3 y WAV flotante intactos; una muestra distinta se detecta) y `tests/test_process_job_contrato.py`.

## 7.6.15 (5-oct-2026) — ARCHIVOS RAROS: UN MENSAJE CLARO Y SIN REINTENTOS («todo por sistema»)
- **Antes:** un archivo dañado o que no era audio fallaba al decodificar con un error técnico (`LibsndfileError…`, `NoBackendError`). Se reintentaba 3 veces y terminaba en «sin respuesta después de 3 intentos».
- **Ahora:**
  - `sondear_audio` mira el archivo con `ffprobe` antes de cargarlo (rápido, sin decodificar). Sin audio legible, o con menos de 1 s, termina al instante con `determinista:` y un mensaje para el DJ:
    - «No pudimos leer el audio de este archivo: está dañado o no es un formato de audio. Expórtalo de nuevo en MP3, WAV, AIFF o FLAC y vuelve a subirlo.»
    - «El archivo dura menos de 1 segundo…»
  - Si igual falla la decodificación en `analyze`, también es determinista, con el mismo mensaje.
  - El archivo de más de `MAX_TRACK_MB` también dice en castellano cuánto pesa y qué hacer.
  - **Más largo que el análisis (10 min, por ejemplo un set):** se analiza como hasta ahora, pero queda la bandera `analisis_parcial:primeros_600_s_de_<total>`.
- Falta del lado de la plataforma: que `worker-result` cierre en `error` al primer `determinista:` (hoy reintenta hasta 3) y que la Biblioteca muestre el mensaje sin el prefijo.
- Pruebas: `tests/test_archivos_raros.py`, con archivos hechos con ffmpeg.
  - **Válidos** (WAV de 24 bits a 96 kHz, AIFF, FLAC, MP3 CBR y VBR, M4A, MP3 con portada de 3000×3000): se analizan.
  - **Inválidos** (bytes al azar, texto, vacío, imagen, solo cabecera): error claro en menos de 10 s y sin llegar a `analyze`.
  - **Dudosos** (ID3 con tamaño imposible, MP3 cortado): `done` o error claro, nunca otra cosa.

## 7.6.14 (5-oct-2026) — EL SET SE ARMA POR TRAMOS A DISCO (#75 C-2)
- Medido el 5-oct (GitHub Actions, 28 temas reales en largo y BPM, 163 min de set): el render llegaba a **13,97 GB** de RAM, en una réplica de 10 GB. `render_set` armaba el set entero en memoria y cada transición lo copiaba (`np.vstack`), y el máster hacía más copias del set completo.
- `_mezcla_plan` ahora escribe a disco (`SetEnDisco`, float32 estéreo) todo lo anterior al punto de cada transición. En memoria queda solo el tema que suena. Las cifras de cada transición (mezcla, eco, corte, encadenado) no cambian.
- `_masterizar_y_subir` recorre el set en trozos de 30 s, en dos pasadas: sonoridad BS.1770 y pico por trozos (`_lufs_y_pico`, mismo resultado que pyloudnorm) y después ganancia + WAV por trozos. El comando de ffmpeg es el mismo.
- El original de cada tema se borra apenas se decodifica. Disco necesario: ~1,9 GB por hora de set (float32 + WAV), en lugar de RAM.
- El método sin plan (`_mezcla_libre`, sets viejos) sigue en memoria. Solo se usa si el plan no coincide.
- Pruebas: `tests/test_set_memoria.py` (el pico no crece con el set; falla con 7.6.13). El banco de 28 temas se repite en la acción «Banco set largo».

## 7.6.13 (5-oct-2026) — LOS TEMAS DEL TALLER SALEN «CONSTANTE» Y NO A 149 · Refs dj-connect#618
- **Qué pasaba:** los temas que exporta el Taller (100 BPM exactos, Entrada de 19,2 s sin bombo, voz) quedaban `tempo_stability = variable` (residuo 136–170 ms), con `bpm_fuera_de_rango:99.959` y confianza 0,6. Con la 7.6.12, además, `detect_tempo` daba **149** (×1,5). El bombo cae a ~1 ms de una rejilla fija de 100.
- **Causa 1 (149, regresión de 7.6.12):** la regla de #206 es simétrica. La octava gruesa (99,3; puntaje 0,087) quedó «explicada» por la media rejilla de 148,85 (0,0796), y con eso 149 ganaba igualando dentro del 10 %. Con la 7.6.11 los mismos WAV daban 99,28 y 99,0.
- **Causa 2 (variable):** `beat_track` arranca en 100,35 (el lag entero del tempograma a hop 128), sigue la envolvente de banda ancha y se pasea por el contratiempo con saltos de vuelta. El residuo es del rastreador, no del audio.
- **Arreglo 1 (`grid_detect.desempate_por_bombo`):** cuando dos tempos están en 3:2, decide el bombo. Se toman sus ataques (banda 35–130 Hz, igual que el ancla CM2) y gana la rejilla que junta un 25 % más, buscando en ±1,5 %. Medido: Taller 100 → 58 y 72 % contra 149 → 39 y 36 %; sintético de 136 → 100 % contra 34 %. Si no hay bombo (banda por debajo de −20 dB) o no los separa, siguen las reglas de #206.
- **Arreglo 2 (`worker.ajuste_por_bombos`):** si el rastreador deja el tema «variable», el ajuste lineal se repite sobre los golpes graves, en la rejilla de medio tiempo (el bajo a contratiempo también marca el tempo), y gana el de menor residuo. **Lo que hoy sale «constante» no cambia.** Un bombo que de verdad se acelera (98 → 102) sigue «variable».
- **Memoria:** los ataques del bombo se buscan a ~2 kHz: +0,2 GB en un tema de 10,5 min, lejos del pico de 2,8 GB del análisis. Solo corre en los temas que el rastreador deja «variable» o en un empate 3:2.
- **`bpm_fuera_de_rango`** ahora tiene medio BPM de holgura (99,5–150,5): 99,959 es un tema de 100.
- **Con los WAV reales** (bajados de Storage solo para leer): «If not today…» 99 · residuo 170 → **6,7 ms** · constante · sin banderas. «Método 2 · v1»: 149 → **99,28** · residuo 136,1 → **2,7 ms** · `bpm_precise` **100,0** · sin banderas. Pruebas: `tests/test_618_taller_tempo.py`. Las de comportamiento fallan con 7.6.12 (149,0 y residuo de 105,7 ms).

## 7.6.12 (4-oct-2026) — 136 BPM SIN ETIQUETA YA NO SALE A 2/3 · Refs dj-connect#206
- **Qué pasaba:** un tema sin BPM en la etiqueta a 136, con bombo en cada tiempo y hi-hat a contratiempo, salía **90,54** (`tempo_stability` «desconocido»). La semilla ×2/3 de 7.6.9 (#17) entraba en la búsqueda (90,7 ≥ 90), y su rejilla de 1,5 tiempos = **3 medios tiempos** caía siempre sobre un golpe (bombo, hi-hat, bombo…): puntuaba más del 10 % sobre el tempo real.
- **Causa medida en el CI** (`tests/test_206_diagnostico.py`, luego retirado): librosa da **92,3**, que queda como «octava» y se ajusta a **90,60** (puntaje de rejilla 0,7444); 136 entra como la proporción ×1,5 y se ajusta a **135,70** (0,7427, un 0,2 % menos). Tenía que ganar por más de un 10 % (`VENTAJA_PROPORCION`): perdía.
- **Arreglo (`grid_detect.explicada_por_media_rejilla`):** si los golpes de la octava caen sobre la rejilla de **medio tiempo** de la proporción (90,7 cada 1,5 tiempos = 3 medios tiempos de 136), la proporción los explica todos y además pega en cada tiempo: se invierte la carga de la prueba y es la **octava** la que tiene que ganar por la misma `VENTAJA_PROPORCION` para quedarse. Un tema de 120 de verdad no cambia: su ×1,5 (180) solo pega en uno de cada tres puntos y la octava le gana por mucho más. En el sentido contrario (una proporción explicada por la media rejilla de la octava) la proporción no compite. «Right Thing» (123 contra 174,33: 2,835 medios tiempos) y Day 'N' Nite (×0,75 incorrecta) siguen igual: necesitan el 10 %.
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
