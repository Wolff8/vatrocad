# VatroCAD

A live, dispatch-style console for fire-service and emergency-medical
interventions in **Croatia** (fire + hitna) and **Austria** (Lower and Upper
Austria fire dispatch, with Slovenian explanations of every alarm code) —
chronological log, status pills (active / contained / closed), a geo plot per
country, and a real-time active-incidents strip. Not a news feed: built to
read like a CAD system, sourced from the public brigade, county (ŽVOC),
police, HVZ and Landesfeuerwehrverband pages that publish per-call data.

```
  brigade / police / HVZ pages          (public sources)
              │
   vatrocad.py --once   ← GitHub Actions cron, every 15 min   (WRITE, service_role key)
              │  upsert
              ▼
   Supabase Postgres  ·  public.incidents  ·  row-level security
              │  REST + realtime websocket (READ, publishable key)
              ▼
   vatrocad/index.html  on GitHub Pages    (the dispatch console)
```

- **`vatrocad/vatrocad.py`** — stdlib-only Python poller. Fetches the sources,
  parses per-call Croatian text into structured incidents, and upserts them
  into Supabase. `python3 vatrocad.py --once` runs one poll-and-push cycle
  (used by the GitHub Actions cron); no args starts a local server + dashboard
  on `http://127.0.0.1:8713` for running entirely offline of Supabase.
- **`vatrocad/index.html`** — the console itself. Reads Supabase directly:
  realtime websocket for instant updates, REST polling as an automatic
  fallback. Uses only the read-only publishable key, safe to ship in a static
  page.
- **`vatrocad/README.md`** — full source list, the 12-hour live / 3-day recent
  window rule, and notes on what's deliberately *not* included (nothing that
  requires bypassing authentication; no satellite fire detections — this is
  interventions, not detections).
- **`DEPLOY.md`** — step-by-step to wire up Supabase secrets and GitHub Pages.
- **`.github/workflows/`** — the Actions workflows: the poller cron (with a
  cached geocode database between runs), the Pages deploy, and `probe.yml`, a
  manual helper that fetches any URL from the runner's network and prints it
  into the job log — for inspecting candidate sources that the development
  sandbox cannot reach.

Rows carry a `country` column (`HR` / `AT`). Croatian rows are kept as an
archive and merely hidden after three days; Austrian rows are **deleted** after
three days (and a running call that vanished from its live list is deleted
after 40 minutes), because those feeds are an operational picture, not a
record.

## Quick start

See `DEPLOY.md`. Short version: add `SUPABASE_URL` and `SUPABASE_SERVICE_KEY`
as repo secrets, turn on Pages (Source: GitHub Actions), push. The poller
keeps Supabase current every 15 minutes; the console reads it live.

## A note on sources

Every source here is a public page — a brigade's own site, a county fire
association's bulletin page, a police administration's news feed, HVZ's
public digest. Croatia's actual dispatch systems (UVI/VATROnet, Zagreb's
brigade database) are closed and credentialed; this project does not attempt
to access them. See `vatrocad/README.md` for the full list and reasoning.
