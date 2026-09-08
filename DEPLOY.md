# Publishing VatroCAD (Supabase + GitHub, all free)

The system has three pieces:

```
  brigade / police / HVZ pages          (public sources)
              │
   vatrocad.py --once   ← GitHub Actions cron, every 15 min   (WRITE, service_role key)
              │  upsert
              ▼
   Supabase Postgres  ·  public.incidents  ·  row-level security
              │  REST select (READ, publishable key)
              ▼
   index.html on GitHub Pages            (the dispatch console)
```

The **poller** writes with the secret `service_role` key (kept in CI only).
The **console** reads with the `publishable` key (safe in the browser; RLS
allows SELECT and nothing else).

---

## What already exists

- Supabase project `ofzwkanpjhdvlygrhbum` — table `public.incidents` created,
  RLS on (public read, no public write), realtime enabled, 58 rows seeded.
- `index.html` — the console, already wired to this project's URL + publishable key.
- `vatrocad.py` — poller; upserts on every `--once` run when the two env vars are set.

You only need to wire up the two GitHub workflows.

---

## Step 1 — Get the two keys from Supabase

Supabase dashboard → **Settings → API**:

| Key | Where it goes | Purpose |
|-----|---------------|---------|
| Project URL (`https://ofzwkanpjhdvlygrhbum.supabase.co`) | Actions secret `SUPABASE_URL` | poller target |
| `service_role` secret | Actions secret `SUPABASE_SERVICE_KEY` | poller WRITE (bypasses RLS) |
| `publishable` (already in `index.html`) | nothing to do | console READ |

⚠️ The `service_role` key is a full-access key. Put it **only** in GitHub Actions
secrets. Never commit it, never place it in `index.html`.

## Step 2 — Put the files in the repo

```
<repo root>/
  vatrocad/
    vatrocad.py
    index.html
    README.md
  .github/workflows/
    vatrocad-poll.yml     (from deploy/workflows/)
    vatrocad-pages.yml    (from deploy/workflows/)
```

## Step 3 — Add the Actions secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**:

- `SUPABASE_URL` = `https://ofzwkanpjhdvlygrhbum.supabase.co`
- `SUPABASE_SERVICE_KEY` = the `service_role` key from Step 1

## Step 4 — Turn on Pages

Repo → **Settings → Pages → Build and deployment → Source: GitHub Actions**.

## Step 5 — Push and verify

- Actions tab → **VatroCAD poll** → *Run workflow* to poll immediately (or wait 15 min).
- Actions tab → **VatroCAD pages** runs on push; the URL prints at the end
  (`https://<user>.github.io/<repo>/`).
- Open the URL. The header should read `supabase · N rows · synced HH:MM`.

---

## Notes

- **Cadence.** The poller runs every 15 min. Do not go below ~5 min — these are
  small volunteer-brigade servers, and `vatrocad.py` already uses conditional GET
  so unchanged pages cost them almost nothing.
- **Freshness.** The console shows the 12-hour live band by default and caps the
  log at 3 days; older Croatian rows stay in the table but are hidden. Austrian
  rows (`country = 'AT'`) are deleted by the poller once they pass 3 days.
- **Geocode cache.** The poller restores `vatrocad/vatrocad.sqlite3` from the
  Actions cache (`Restore geocode cache` step) so Austrian towns are not
  re-geocoded on every run. A cold cache costs ~2 minutes on the first run.
- **Probing a new source.** Actions tab → **probe URL** → *Run workflow* with the
  URL; the job log shows the response headers and the first N bytes of the body
  as seen from a GitHub runner.
- **Cost.** Supabase free tier and GitHub Actions/Pages free minutes cover this
  comfortably. A Supabase free project pauses after ~1 week with no traffic; the
  15-min poller keeps it awake.
- **Realtime (optional upgrade).** The table is in the `supabase_realtime`
  publication, so the console could subscribe over websockets for instant
  updates instead of the 30-second poll. Not required — the current polling
  approach is simpler and works on plain GitHub Pages.
