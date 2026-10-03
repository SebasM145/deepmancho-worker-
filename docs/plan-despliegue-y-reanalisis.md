# Cuando vuelva el plan de Railway: despliegue, reanálisis y verificación

Preparado en la noche del 2→3-oct-2026 (rol Workers), a pedido del integrador. Railway está sin plan: hasta que se active, nada de esto corre. **Todo lo de este documento lo hace el integrador de día.** El SQL de abajo es para la base de producción.

## 1. Qué está en `main` y qué espera en PR

| Pieza | Dónde | Versión | Redespliega |
|---|---|---|---|
| Tempo sin BPM previo, ±8 % y semilla de 2/3 (#206) | main (#10, #17) | análisis 7.6.4 | `deepmancho-worker-` |
| Carga masiva: tope por trabajo, `MAX_TRACK_MB`, `no_anchor` como carrera | main (#14) | análisis 7.6.2 · grid-verifier 1.1.2 · stems 1.23.2 | los tres |
| Género por etiqueta, etapa A | main (#16) | análisis 7.6.3 | `deepmancho-worker-` |
| `energy_v2` aparte (#248) | **PR #21** | análisis 7.6.5 | `deepmancho-worker-` |
| Tiempo por trabajo en el log y archivo muy grande como determinista | **PR #22** | análisis 7.6.6 | `deepmancho-worker-` |
| Tempo más rápido con el mismo resultado (`analyze` 35 → 18 s de CPU en un tema de 10 min) | **PR #24** | análisis 7.6.7 | `deepmancho-worker-` |
| Reanálisis en silencio (`analysis_jobs.origen`) | **plataforma #404** | SQL | — |
| Columna `music_tracks.energy_v2` | **plataforma #405** | SQL | — |
| Pista casi vacía en `stem_quality` | PR #20 | stems 1.23.3 | `stems-worker` |

**Orden recomendado:** SQL de #404 y #405 (pruebas revertidas en cada PR) → #21 → #22 → #24 (chocan solo en el banner y el CHANGELOG) → un solo redespliegue de los tres servicios. #20 puede ir en el mismo redespliegue de stems.

**Antes de redesplegar:** confirmar en Railway qué versión arrancó por última vez (la primera línea del log: `DeepMancho worker iniciado (vX…)`). Si es anterior a 7.6.2, este despliegue trae también la carga masiva y el género.

## 2. Reanálisis por #206 (tempo)

### Qué canciones
El bug solo afecta a temas **sin BPM previo** (`bpm_source='analyzed'` o vacío, y sin `bpm_tag`). Los temas con etiqueta, BPM manual o `bpm_tag` usan ese BPM como semilla y no cambian.

- **Tanda 1 (segura):** `analyzed` + `tempo_stability='variable'`. Es la huella del bug: la búsqueda chocaba con el borde y el ajuste salía «variable». En la medida con 6 temas reales, los 3 que salían mal (132,7 · 115 · 125,2 en vez de 124 · 123 · 125) estaban todos en «variable».
- **Tanda 2 (opcional, después de revisar la 1):** `analyzed` + `constante`, con el tempo en una zona sospechosa (125–126,5, 129–133 o más de 150). Ahí caen los errores con la semilla de 2/3 (123 → 164 o 174).

Quedan fuera los temas en la papelera, las pistas vacías y las pistas de una toma del Estudio (`package_id`): esas heredan la rejilla de su canción madre con `heredar_rejilla_de_toma`.

### Paso 1 · contar (solo lectura)
```sql
select
  count(*) filter (where t.tempo_stability = 'variable') as tanda_1,
  count(*) filter (where t.tempo_stability = 'constante'
    and ((t.bpm + coalesce(t.bpm_fine, 0)) between 125 and 126.5
      or (t.bpm + coalesce(t.bpm_fine, 0)) between 129 and 133
      or (t.bpm + coalesce(t.bpm_fine, 0)) > 150)) as tanda_2,
  round(avg(t.duration_seconds) / 60.0, 1) as minutos_promedio
from music_tracks t
where t.deleted_at is null and not coalesce(t.pista_vacia, false)
  and t.package_id is null
  and coalesce(t.bpm_source, 'analyzed') = 'analyzed' and t.bpm_tag is null
  and coalesce(t.grid_source, '') <> 'rekordbox';
```

### Paso 2 · respaldo (antes de encolar)
```sql
create table if not exists respaldo_reanalisis_206 as
select t.id, t.bpm, t.bpm_fine, t.bpm_precise, t.bpm_detected, t.bpm_source, t.tempo_stability,
       t.tempo_residual_ms, t.first_beat_offset_ms, t.first_beat_detected_ms, t.grid_source,
       t.cue_points, t.cue_source, t.energy, now() as respaldado
from music_tracks t
where t.deleted_at is null and not coalesce(t.pista_vacia, false) and t.package_id is null
  and coalesce(t.bpm_source, 'analyzed') = 'analyzed' and t.bpm_tag is null
  and coalesce(t.grid_source, '') <> 'rekordbox'   -- worker-result solo protege 'manual'
  and t.tempo_stability = 'variable';   -- tanda 1
```
El reanálisis **no pisa** la rejilla manual (`grid_source='manual'`) ni los cues manuales, importados o del plan (`decidirCues`). El respaldo es para comparar y, si algo sale mal, volver atrás.

### Paso 3 · encolar
```sql
insert into analysis_jobs (track_id, status, origen)
select r.id, 'pending', 'reanalisis' from respaldo_reanalisis_206 r
where not exists (select 1 from analysis_jobs a
                  where a.track_id = r.id and a.status in ('pending', 'processing'));
```
- Con la clave de servicio (editor SQL), el disparador `cobrar_reanalisis` **no cobra créditos** al DJ (`auth.uid()` es nulo).
- `worker-next` no manda semilla para estos temas: el tempo se mide de cero, que es justo lo que arregla #206.
- **En silencio** (decisión del integrador, 3-oct): con `origen='reanalisis'` no se abre la tanda ni se manda «N canciones analizadas» (plataforma #404). **Requiere aplicar #404 antes de encolar**: sin esa migración, la columna `origen` no existe y el insert falla, que es lo seguro.
- **No mezclarlo con la carga masiva:** la cola es por orden de llegada. Encolar el reanálisis **después** de la carga de ~1.000 temas, o en tandas de 100.

### Cuánto cuesta
- **Tiempo de CPU:** medido en local (Apple M4, 1 hilo), `analyze` gasta 35 s de CPU con un tema de 10 min, y **18 s con #24**. En Railway hay que esperar hasta 3 veces más. Para un tema promedio de 6–7 min, con #24: **~40 s por tema y réplica**.
  - 100 temas con 5 réplicas: **~15 min**. 1.000 temas: **~2–3 h**.
  - El dato real sale del log de #22 (`OK en N s`): medir con las primeras 20 y recalcular.
- **Plata (precios de lista de Railway: US$20 por vCPU al mes y US$10 por GB al mes; verificar el plan que se contrate):** ~1 min de 1 vCPU y ~2 GB de promedio ≈ **US$0,001 por tema**. 1.000 temas ≈ **US$1**. Bajar los originales de Supabase: ~15 MB por tema, ~15 GB por cada 1.000 temas (dentro de la cuota del plan, o ~US$1,35 si se pasa).
- **ElevenLabs y créditos de DJ:** cero. El reconocimiento por huella solo corre si al tema le falta artista o título.

### Paso 4 · comparar y cerrar #206
```sql
select r.id, r.bpm + coalesce(r.bpm_fine, 0) as antes, r.tempo_stability as estab_antes,
       t.bpm + coalesce(t.bpm_fine, 0) as ahora, t.tempo_stability as estab_ahora
from respaldo_reanalisis_206 r join music_tracks t on t.id = r.id
order by abs((t.bpm + coalesce(t.bpm_fine, 0)) - (r.bpm + coalesce(r.bpm_fine, 0))) desc
limit 30;
```
**Se da por bueno si:**
- baja mucho la cantidad de temas en «variable»;
- los cambios grandes son correcciones creíbles (por ejemplo, 132,7 → 124 o 115 → 123), revisadas de oído en 10 temas;
- ningún tema con candado cambió de BPM.

Si algo sale mal, vuelve atrás con un `update … from respaldo_reanalisis_206`, solo en las columnas de tempo, rejilla y cues.

## 3. #248 (energía): sin reanálisis por ahora
- `energy` **no cambia** con #21: la v2 solo se envía aparte, y todavía no está calibrada.
- Para guardarla hacen falta la columna `music_tracks.energy_v2 smallint` y una línea en `worker-result` (Funciones). Sin eso, la v2 solo aparece en el log (`energia: 8 (v2: 6)`).
- **No hace falta reanalizar nada para comparar:** la carga de ~1.000 temas trae suficientes datos de las dos.
- Reanalizar toda la biblioteca para cambiar `energy` cuesta lo mismo por tema que el punto 2. **Solo se hace cuando Germán decida**, con la referencia de ~15 temas que pidió Workers en #248.

## 4. Lista de verificación después de desplegar
**Arranque (log de cada servicio):**
- [ ] Análisis: `DeepMancho worker iniciado (v7.6.6: …` (o la última que haya entrado).
- [ ] grid-verifier dice 1.1.2 y stems dice 1.23.2, o 1.23.3 si entró #20.
- [ ] Ninguno muestra un traceback al arrancar.

**Primer tema subido (uno con BPM conocido, sin etiqueta de BPM):**
- [ ] El log muestra `v7 bpm X → Y (…, constante)` con Y a menos de 0,1 del BPM real.
- [ ] Aparece `OK en N s (tema de M s)` (#22), con N bien por debajo de 420.
- [ ] Aparece `energia: A (v2: B)` (#21).
- [ ] Aparece `genero: <género> (<confianza>)`.
- [ ] En la app, el tema muestra su BPM, su tonalidad, los cues y la rejilla alineada al bombo.

**Carga masiva (primera tanda de 20–50 temas):**
- [ ] El N más alto de `OK en N s` (con #24 se espera ~1 min en un tema de 10 min): si pasa de ~350 s, hay que subir `TOPE_TRABAJO_S` (que no llegue a los 8 min del reclamo) o bajar `MAX_DURATION`, antes de seguir.
- [ ] No hay `trabajo mas largo que 7 min` en el log.
- [ ] Las memorias de las réplicas en Railway no se acercan al límite (el pico de `analyze` con un tema de 10 min es ~4,5 GB, medido en local).
- [ ] `select status, count(*) from analysis_jobs where created_at > now() - interval '2 hours' group by 1;` sin `error` acumulándose.
- [ ] Los temas con etiqueta de género llegan a la carpeta de su género; los que no la tienen, a «Por revisar».
- [ ] Los temas en `error` con `determinista:archivo de …` son archivos de más de `MAX_TRACK_MB` (250). Esos se resuben en MP3.

**Reanálisis de #206:** el punto 2, pasos 1 a 4.
