# Historial de versiones · worker.py (análisis)

## 7.5 (30-sep-2026) — NINGÚN TEMA SIN HOT CUES
- Plan A sin cambios: `detect_cues` (metodología MIK sobre el ancla definitiva). En la biblioteca de DJ da 8 cues en 1.109 de 1.113 temas.
- **Plan B** `cues_respaldo`: si el detector no encuentra estructura (menos de 24 compases, poco contraste), cues sobre la rejilla de frases desde el ancla real: A en 0, H en la última frase que deja cola (16 compases o ¼ del tema, prefiriendo caída de energía) y hasta 6 intermedios en los bordes con más cambio medido. Frase de 8 compases (4 o 2 en audio corto).
- **Plan C** `cues_por_tiempo`: sin BPM, A en 0 y H al 85 %.
- B y C van con `confidence` < 0.5 y `origen` (`grilla` / `tiempo`): el mezclador no los usa para mezcla automática; el DJ puede saltar a ellos o corregirlos a mano.
- El resultado incluye `duration_seconds` (si el audio no se cortó en MAX_DURATION): muchos temas generados no la tenían.
- Pruebas: `tests/test_cues_respaldo.py` (boceto de 16 compases, rejilla de frase, audio mínimo, sin BPM, energía 1–10).

# Historial de versiones · stems_worker.py

## 1.23.0 (30-sep-2026) — MOTOR HÍBRIDO DE SEPARACIÓN (APAGADO POR DEFECTO)
- **Nada cambia si no se pide**: sin `STEMS_ENGINE` y sin `model: "hibrido"` en el trabajo, separa Demucs igual que la 1.22.2.
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
