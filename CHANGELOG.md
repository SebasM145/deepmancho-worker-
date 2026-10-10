# Historial de versiones · worker.py (análisis)

## 7.6.22 (10-oct-2026) — UN TEMA REANALIZADO Y LIMPIO BORRA SUS ALARMAS VIEJAS (#206)
- **Qué pasó:** después del reanálisis del 9-oct, `02589893` y `085906cd` quedaron bien (128,071 y 129,993, «constante», confianza 1), pero `analysis_flags` conservó las alarmas del análisis viejo: `bpm_fuera_de_rango:160.007`, `bpm_fuera_de_rango:162.502` y `tempo_variable`. Cualquier pantalla que lea las alarmas mostraba un aviso falso.
- **Causa (worker, no `worker-result`):** el worker solo mandaba `analysis_flags` si la revisión encontraba algún problema. `worker-result` reemplaza las alarmas cuando llega el campo y, si no llega, no toca la columna. Un tema limpio no mandaba nada y se quedaba con las de antes.
- **Arreglo:** cuando la revisión automática corre, `analysis_flags` va siempre, vacía si no hay problemas. `worker-result` ya guarda la lista vacía (más su propia bandera `bpm_etiqueta_difiere`, si aplica). No hace falta tocar funciones ni SQL.
- **Sin cambio:** si la revisión no llega a correr (falla el análisis v7), el campo no va y las alarmas guardadas se conservan, como antes. Las banderas que suma `process_job` (`analisis_parcial`, `bpm_etiqueta_octava`, `genero_por_revisar`) se siguen sumando a la lista.
- **Para cerrar #206:** después de desplegar, reanalizar en silencio los 2 temas (`origen='reanalisis'`); deben quedar con `analysis_flags = []`.
- **Mezclar después de #50 (7.6.21):** los dos tocan la línea de arranque y el inicio de este historial.
- Pruebas: `tests/test_alarmas_reanalisis.py` (2). Con 7.6.20 falla la del tema limpio.

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
