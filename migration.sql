-- Cola de análisis + RPC de reclamo atómico. Se aplica en el proyecto Supabase de DeepMancho.
-- (Lovable la aplica automáticamente; este archivo es la referencia.)

create table if not exists public.analysis_jobs (
  id uuid primary key default gen_random_uuid(),
  track_id uuid not null references public.music_tracks(id) on delete cascade,
  status text not null default 'pending' check (status in ('pending','processing','done','error')),
  attempts int not null default 0,
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists analysis_jobs_status_idx on public.analysis_jobs (status, created_at);
create index if not exists analysis_jobs_track_idx on public.analysis_jobs (track_id);

alter table public.analysis_jobs enable row level security;

-- El DJ dueño puede ver/crear jobs de sus tracks (para encolar desde el frontend y ver estado).
drop policy if exists "dj sees own jobs" on public.analysis_jobs;
create policy "dj sees own jobs" on public.analysis_jobs for select
  using (exists (select 1 from public.music_tracks mt where mt.id = analysis_jobs.track_id and is_dj_owner(mt.dj_id)));

drop policy if exists "dj inserts own jobs" on public.analysis_jobs;
create policy "dj inserts own jobs" on public.analysis_jobs for insert
  with check (exists (select 1 from public.music_tracks mt where mt.id = analysis_jobs.track_id and is_dj_owner(mt.dj_id)));

-- Reclamo atómico de un job (lo usa el worker con la service key; SECURITY DEFINER).
create or replace function public.claim_analysis_job()
returns setof public.analysis_jobs
language plpgsql
security definer
set search_path = public
as $$
declare
  jid uuid;
begin
  select id into jid from public.analysis_jobs
    where status = 'pending'
    order by created_at
    for update skip locked
    limit 1;
  if jid is null then
    return;
  end if;
  return query
    update public.analysis_jobs
      set status = 'processing', attempts = attempts + 1, updated_at = now()
      where id = jid
      returning *;
exception when others then
  raise warning 'claim_analysis_job failed: %', sqlerrm;
  return;
end;
$$;
