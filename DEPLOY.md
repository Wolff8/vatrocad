# Publishing VatroCAD (Supabase + GitHub, all free)

The system has three pieces:

```
  public brigade / county / police / newsroom / Austrian LFV + LPD pages
              │
   vatrocad.py --serve-minutes 55 --interval 600      (WRITE, service_role key — CI secret only)
   GitHub Actions: one 55-min job at a time, polls every 10 min,
   hands over to its own successor; hourly cron = fallback
              │  delta upsert + control stamps + source_status + AT prune
              ▼
   Supabase Postgres · public.incidents / control / source_status · rpc request_poll()
   row-level security: public SELECT, nothing else
              │  PostgREST + realtime (READ, publishable key)   ▲ request_poll() ("Osveži")
              ▼                                                  │
   vatrocad/web → npm ci / test / build → GitHub Pages   https://wolff8.github.io/vatrocad/
```

The **poller** writes with the secret `service_role` key (kept in CI only).
The **console** reads with the `publishable` key (safe in the browser; RLS
allows SELECT and nothing else) and can call one RPC, `request_poll()`, which
does nothing but stamp a timestamp.

---

## What already exists

- Supabase project `ofzwkanpjhdvlygrhbum` (eu-west-1): tables
  `public.incidents`, `public.control` (one row), `public.source_status`,
  function `public.request_poll()`, RLS on with public-read policies,
  `incidents` in the `supabase_realtime` publication. The SQL below recreates
  all of it.
- `vatrocad/web/src/config.ts` — `SB_URL` and `SB_KEY` (the publishable key)
  for this project. Change both if you point the console at another project.
- `vatrocad/vatrocad.py` — pushes whenever `SUPABASE_URL` and
  `SUPABASE_SERVICE_KEY` are set.
- The three workflows in `.github/workflows/`.

---

## Step 1 — Get the keys from Supabase

Supabase dashboard → **Settings → API**:

| Key | Where it goes | Purpose |
|---|---|---|
| Project URL (`https://<ref>.supabase.co`) | Actions secret `SUPABASE_URL` | poller target |
| `service_role` secret | Actions secret `SUPABASE_SERVICE_KEY` | poller WRITE (bypasses RLS) |
| `publishable` key | `vatrocad/web/src/config.ts` (`SB_URL`, `SB_KEY`) | console READ + `request_poll()` |

The `service_role` key is a full-access key. Put it **only** in GitHub Actions
secrets. Never commit it, never put it anywhere under `vatrocad/web`.

## Step 2 — Supabase objects

Run this in the SQL editor of a fresh project (it matches the live project
object for object). `incidents` is what the poller upserts and the console
reads; `control` is the one-row handshake between the two; `request_poll()`
is the only write the publishable key can cause; `source_status` is optional
(both sides cope without it).

```sql
-- incidents: one row per parsed call/report/warning
create table if not exists public.incidents (
  id            text primary key,
  source        text not null,
  region        text,
  country       text not null default 'HR',
  ref           text,
  occurred      date,
  occurred_time text,
  ts            timestamptz,
  category      text,
  status        text,
  title         text,
  location      text,
  lat           double precision,
  lon           double precision,
  units         text,
  crew          integer,
  vehicles      integer,
  raw           text,
  link          text,
  title_sl      text,
  raw_sl        text,
  cluster       text,
  first_seen    timestamptz default now(),   -- set by the database on insert (the NOVO badge)
  last_seen     timestamptz default now()    -- set by the poller on every push (incremental loads)
);
create index if not exists incidents_last_seen_idx        on public.incidents (last_seen desc);
create index if not exists incidents_occurred_country_idx on public.incidents (country, occurred desc);
create index if not exists incidents_ts_idx               on public.incidents (ts desc);
create index if not exists incidents_country_idx          on public.incidents (country);
create index if not exists incidents_region_idx           on public.incidents (region);
create index if not exists incidents_category_idx         on public.incidents (category);
alter table public.incidents enable row level security;
create policy "public read incidents" on public.incidents
  for select to anon, authenticated using (true);
alter publication supabase_realtime add table public.incidents;

-- control: the single handshake row (id is forced to 1)
create table if not exists public.control (
  id                integer primary key default 1,
  poll_requested_at timestamptz,   -- stamped by request_poll() (the console's Osveži)
  poll_started_at   timestamptz,   -- stamped by the poller before a cycle
  poll_finished_at  timestamptz,   -- stamped by the poller after a cycle whose push succeeded
  runner_until      timestamptz,   -- the running job's deadline; null when no job is up
  note              text,          -- human-readable state, in Slovenian
  constraint control_single check (id = 1)
);
insert into public.control (id) values (1) on conflict (id) do nothing;
alter table public.control enable row level security;
create policy control_read on public.control
  for select to anon, authenticated using (true);

-- request_poll(): callable with the publishable key; throttled to once per 60 s
create or replace function public.request_poll()
returns timestamptz
language plpgsql
security definer
set search_path = public
as $$
declare
  cur timestamptz;
begin
  select poll_requested_at into cur from public.control where id = 1;
  if cur is not null and cur > now() - interval '60 seconds' then
    return cur;
  end if;
  update public.control set poll_requested_at = now() where id = 1
    returning poll_requested_at into cur;
  return cur;
end;
$$;
grant execute on function public.request_poll() to anon, authenticated;

-- source_status: one row per source, rewritten after every poll cycle
create table if not exists public.source_status (
  source     text primary key,
  country    text,                          -- not written by the poller; the console infers it
  ok         boolean not null default true,
  rows       integer not null default 0,
  ms         integer,
  error      text,
  checked_at timestamptz not null default now()
);
alter table public.source_status enable row level security;
create policy "public read source_status" on public.source_status
  for select to anon, authenticated using (true);
```

The default Supabase table grants to `anon`/`authenticated` stay as they are;
with RLS enabled and only SELECT policies, the publishable key cannot insert,
update or delete anything. The poller's `service_role` key bypasses RLS.

## Step 3 — Files in the repo

```
<repo root>/
  vatrocad/
    vatrocad.py                 the poller
    README.md
    web/                        the console (package.json, package-lock.json, src/, public/, …)
  .github/workflows/
    vatrocad-poll.yml           poller chain
    vatrocad-pages.yml          Pages build + deploy
    probe.yml                   manual URL probe
```

`vatrocad/vatrocad.sqlite3` is git-ignored; in CI it lives in the Actions
cache (Step 6, *Cache DB*).

## Step 4 — Add the Actions secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**:

- `SUPABASE_URL` = `https://<ref>.supabase.co`
- `SUPABASE_SERVICE_KEY` = the `service_role` key from Step 1

Nothing else is secret. `NOMINATIM_BUDGET` is set by the workflow itself
(15 on a cold cache, otherwise the script's default of 25).

## Step 5 — Turn on Pages

Repo → **Settings → Pages → Build and deployment → Source: GitHub Actions**.

`vatrocad-pages.yml` needs no further configuration: it checks out the repo,
`actions/setup-node@v4` with Node 20 and the npm cache keyed on
`vatrocad/web/package-lock.json`, then in `vatrocad/web` runs `npm ci`,
`npm test` (vitest) and `npm run build` (`tsc --noEmit` + `vite build`, base
path `/vatrocad/`), and deploys `vatrocad/web/dist` with
`actions/deploy-pages@v4`. A failing test or type error stops the deploy. It
runs on every push to `main` that touches `vatrocad/web/**` (or the workflow
file) and on *Run workflow*; changes to the poller alone do not redeploy the
page. The site is `https://<user>.github.io/<repo>/` — the base path is
hard-coded to `/vatrocad/` in `vite.config.ts` (which also stamps it into the
generated service worker) and in `public/manifest.webmanifest`, so a repo with
another name needs both changed.

## Step 6 — Start the poller and verify

- Actions tab → **VatroCAD poll** → *Run workflow*. That run polls immediately
  and, 55 minutes later, dispatches the next one; from then on the chain keeps
  itself alive (see *Scheduling* below). The hourly cron also starts a run if
  none is queued.
- Actions tab → **VatroCAD pages** runs on push; the URL prints at the end.
- Open the URL. Within a minute the header should read
  `v živo · pobrano pred N min · pobiralnik teče` (or `osveževanje …` if the
  realtime websocket is blocked — polling still works). Press `Osveži`: the
  toast `Pobiram s virov…` should be followed within a minute or two (the
  poller notices the request within ~15 s, a full cycle takes 30–90 s) by
  `Sveže s virov · ni novega` or `+N novih · skupaj M`.
- The `Viri` button in the stats strip lists every source with its last
  event and, once `source_status` is being written, `preverjeno pred N min`
  and any error text.

---

## Notes

### Scheduling and hand-over

GitHub's cron is best-effort and, on this repo, dropped most slots (a `*/15`
schedule fired once in hours; even hourly slots were skipped). Scheduling
therefore lives in the job:

- One job at a time (`concurrency: vatrocad-poll`, `cancel-in-progress: false`)
  runs `python3 vatrocad.py --serve-minutes 55 --interval 600`: it polls on
  start, then every 10 minutes (fast sources every cycle, slow ones every
  second cycle), and serves a manual `Osveži` within ~15 s by watching
  `control.poll_requested_at`. `timeout-minutes: 70` is the backstop.
- Its last step, **Hand over to the next run**, calls
  `gh workflow run vatrocad-poll.yml` with the job's own `GITHUB_TOKEN`
  (allowed for `workflow_dispatch`; the workflow has `actions: write`). The
  queued run waits behind the concurrency group and starts the moment this one
  exits, so the poller is down only for the runner start-up (~20 s).
- Guards against a runaway chain: the hand-over runs only when the poll step
  actually ran (success or failure, never on cancel); if the job is younger
  than 5 minutes it sleeps out the remainder first (a script crashing on
  start-up can re-dispatch at most ~12 times an hour); and it skips the
  dispatch when a run is already `queued`/`pending`/`waiting`.
- The single cron `3 * * * *` only restarts the chain if a runner dies
  mid-flight; while a run is live the cron run just queues behind it.
- **To stop everything:** Actions → VatroCAD poll → *…* → *Disable workflow*,
  then cancel the running job. Re-enable and *Run workflow* to restart.
- **Cadence.** Do not go below `--interval 300` (the script enforces that
  floor in `--serve-minutes` mode). These are small volunteer-brigade servers;
  conditional GET means an unchanged page costs them a `304`.

### Cache DB

`vatrocad/vatrocad.sqlite3` holds the Nominatim geocode cache, article
bodies, ETag/Last-Modified validators, the translation cache and the content
hashes that drive the delta push. Runners are ephemeral, so the workflow
restores it with `actions/cache/restore@v4` (`restore-keys: vatrocad-db-`
picks the most recent), reports whether a cache was hit (a cold start warns
and sets `NOMINATIM_BUDGET=15`), and saves it under a fresh
`vatrocad-db-<run id>` key with `actions/cache/save@v4` `if: always()`, so the
DB survives even a failed poll step. A cold cache costs a few slow cycles
(geocoding and translation are budgeted per cycle and fill in over time),
nothing else. To reset it, delete the `vatrocad-db-*` entries under Actions →
Caches.

### Freshness and retention

The console loads eight days and shows 24 hours by default (up to 7 days via
the window chips). The poller pushes only rows ≤ 8 days old. Croatian rows are
never deleted from Supabase; Austrian dispatch rows are deleted after 3 days
(an NÖ running call 40 minutes after it left the running list), Austrian
report and police rows after 7 days. The local SQLite keeps everything.

### When Supabase is down

Symptoms: the console header shows `ni povezave` and toasts
`Supabase ne odgovarja`; the poll job log prints
`Supabase ne odgovarja (…) — nadaljujem brez sinhronizacije v tem ciklu` and
`… — Supabase ne odgovarja` at the end of each cycle; if `control` is still
reachable, its `note` reads `Supabase ne odgovarja – podatki niso osveženi`
and `poll_finished_at` stops advancing.

What to do: **Supabase dashboard → Settings → General → Restart project**
(if the project shows as *paused*, use *Restore project* instead). Nothing on
the GitHub side needs touching. The running job keeps scraping into SQLite
(every Supabase call has a short timeout — 10 s for `control`, 15 s default,
20 s for an upsert chunk — two retries and a per-cycle circuit breaker, and
the control read backs off to once a minute), the hand-over
keeps the chain alive regardless, and the first cycle that gets through pushes
every row that is new or changed since the last successful push (content
hashes) — the next job's first cycle additionally re-pushes the whole 8-day
window. The console recovers by itself on its next head-check.

### Probing a new source

Actions tab → **probe URL** → *Run workflow* with the URL (and optionally how
many body bytes to print, default 6000). The job runs `curl -sSIL` for the
response headers and then prints the first N bytes of the body as seen from a
GitHub runner (User-Agent `VatroCAD/1.0`), 30-second timeout. This is how a
site the development sandbox cannot reach gets inspected before a parser is
written. Some sites answer differently by origin — Styria's live overview is
unreachable from a runner too, which is how that was established.

### Cost

- **GitHub Actions.** The chain keeps one runner busy almost continuously:
  about 55–57 minutes per job, ~24 jobs a day, ~23 runner-hours a day, roughly
  700 hours a month. That is free only because the repository is **public**
  (Actions minutes are unlimited for public repos). A private repo's free
  allowance (2,000 minutes a month) would be exhausted in about a day and a
  half.
- **GitHub Pages.** Free for a public repo; the built site is ~150 KB gzipped
  plus fixtures and icons.
- **Supabase.** Free tier is ample: the table holds a few thousand rows, the
  console makes one small request a minute plus a realtime channel, the poller
  a handful of requests per cycle. A free project is paused after about a week
  without activity; the poller's traffic keeps it awake, so a paused project
  means the poller has not been running either.
- **Nominatim and MyMemory** are free and used within their published limits
  (one request per second with an identifying User-Agent, cached for good;
  4,000 words a day, cached for good). Map tiles are Esri's public World Dark
  Gray canvas.

### Realtime

The `incidents` table is in the `supabase_realtime` publication and the
console subscribes to `postgres_changes` on it (status `v živo`). If the
websocket is unavailable the page falls back to `osveževanje`: the 60-second
`control` head-check plus a 10-minute full reload. Removing the table from the
publication costs nothing but immediacy.
