# VatroCAD

A live, dispatch-style console for fire-service, rescue, EMS and police
interventions in **Croatia** and the **Austrian border belt** (Lower and Upper
Austria dispatch logs, Styria and Carinthia after-action reports, four state
police press feeds). Slovenian UI, built to read like a CAD screen rather than
a news feed: chronological log with status pills (`AKTIVNO` / `LOKALIZIRANO` /
`ZAKLJUČENO` / `POROČILO`), country tabs, a clustered map, a 24-hour histogram
and a source-health panel.

**Live:** <https://wolff8.github.io/vatrocad/>

Everything it shows comes from public pages — brigade logs, county centres,
police administrations, HVZ, HGSS, the motorway operator, regional and national
newsrooms, the Austrian Landesfeuerwehrverband dispatch pages and the Austrian
Interior Ministry's press RSS. Nothing behind a login is touched.

## Architecture

```
  CROATIA                                      AUSTRIA
  brigade / JVP / DVD logs · ŽVOC bulletin ·   NÖ "Wastl" + OÖ LFV dispatch logs ·
  20 county police PUs · MUP · HVZ DVOC ·      LFV Steiermark report list ·
  HGSS · HAC motorways · Meteoalarm ·          9 border-belt brigade / BFKDO feeds ·
  14 regional + 5 national newsrooms           4 LPD police press feeds
                      │
                      │  conditional GET (ETag / Last-Modified), one request per host
                      │  at a time, 6 workers, article bodies fetched once ever
                      ▼
  vatrocad/vatrocad.py   --serve-minutes 55 --interval 600        (Python 3.11, stdlib only)
  ┌──────────────────────────────────────────────────────────────────────────────┐
  │ GitHub Actions job  (.github/workflows/vatrocad-poll.yml)                     │
  │  · one 55-minute job, polls every 10 min on its own clock                    │
  │    FAST tier every cycle, SLOW tier every second cycle                       │
  │  · watches control.poll_requested_at → a manual "Osveži" is served in ~15 s  │
  │  · last step dispatches its own successor; the hourly cron is only a fallback│
  │  · SQLite cache DB (geocodes, article bodies, HTTP validators, translations, │
  │    content hashes) restored/saved with actions/cache between jobs            │
  └──────────────────────────────────────────────────────────────────────────────┘
                      │  delta upsert: only new/changed rows (content hash), rows
                      │  ≤ 8 days old, full resync once per job — service_role key
                      │  lives ONLY in the Actions secret SUPABASE_SERVICE_KEY
                      ▼
  Supabase Postgres, schema public, RLS = public SELECT only
    incidents ── control (single row id=1) ── source_status ── rpc request_poll()
                      │                                                  ▲
                      │  PostgREST + realtime websocket                  │  "Osveži":
                      │  (publishable key baked into the bundle)         │  request_poll(),
                      ▼                                                  │  then wait for
  vatrocad/web  (Vite 6 + TypeScript, Leaflet + markercluster, supabase-js for realtime)
    built + tested by .github/workflows/vatrocad-pages.yml → GitHub Pages /vatrocad/
```

- The **poller** writes with the `service_role` key (CI secret only). It never
  waits on a dead database: short timeouts, a per-cycle circuit breaker, and
  a content-hash delta push mean an outage costs nothing but a delay — the
  first cycle that gets through catches up.
- The **console** reads with the publishable key over PostgREST and realtime,
  with a 60-second head-check on `control` and a 10-minute full reload as the
  polling fallback. `Osveži` asks the running poller for an immediate cycle
  through the `request_poll()` RPC (no credential in the page; throttled to
  once per minute) and waits for `control.poll_finished_at` to advance.
- Rows carry `country` (`HR` / `AT`). Croatian rows are kept for good (the
  console only loads the last 8 days); Austrian dispatch rows are **deleted**
  after 3 days and Austrian report/police rows after 7, because those feeds are
  an operational picture, not an archive.

## Repository layout

```
README.md                          this file
DEPLOY.md                          step-by-step: Supabase objects, secrets, Pages, scheduling, outages
vatrocad/
  vatrocad.py                      the poller: fetch, parse, geocode, translate, store, push
  README.md                        the poller in depth: every source, cycle, caches, retention, CLI
  vatrocad.sqlite3                 local cache/archive DB (git-ignored; restored from the Actions cache in CI)
  web/                             the console (Vite + TypeScript) — see web/README.md
    src/                           main.ts, data.ts, store.ts, classify.ts, filters.ts, time.ts, ui/…
    public/                        manifest, icons, fixtures/incidents.json (150 real rows for ?fixture=1)
    scripts/make-fixture.py        regenerates the fixture from a captured poller payload
.github/workflows/
  vatrocad-poll.yml                the poller chain (55-min jobs, self hand-over, hourly fallback cron)
  vatrocad-pages.yml               npm ci → npm test → npm run build → deploy dist/ to Pages
  probe.yml                        manual: fetch any URL from a runner and print headers + body
```

The former single-file console (`vatrocad/index.html`) is superseded by
`vatrocad/web` and is being removed; the Pages workflow no longer deploys it.

## Quick links

| Want to… | Go to |
|---|---|
| Use it | <https://wolff8.github.io/vatrocad/> |
| Understand a source, the poll cycle, retention, caches, CLI flags | [`vatrocad/README.md`](vatrocad/README.md) |
| Work on the console (dev server, tests, fixture mode, `src/` layout) | [`vatrocad/web/README.md`](vatrocad/web/README.md) and the frontend section of [`vatrocad/README.md`](vatrocad/README.md#frontend-vatrocadweb) |
| Recreate the deployment (Supabase SQL, secrets, Pages, what to do in an outage) | [`DEPLOY.md`](DEPLOY.md) |
| Inspect a candidate source from a GitHub runner | Actions → **probe URL** → *Run workflow* |
| Poll right now | the `Osveži` button, or Actions → **VatroCAD poll** → *Run workflow* |

## Quick start

`DEPLOY.md` has the full procedure. Short version: create the Supabase objects
(SQL in `DEPLOY.md`), add `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` as
repository secrets, point `vatrocad/web/src/config.ts` at your project, turn
on Pages (Source: GitHub Actions), push. Start the poll workflow once from the
Actions tab; from then on each run hands over to the next.

To run the poller locally against your own database:

```bash
cd vatrocad
SUPABASE_URL=https://<ref>.supabase.co SUPABASE_SERVICE_KEY=… python3 vatrocad.py --once
```

Without the two variables it still polls and stores everything in
`vatrocad.sqlite3`, just without pushing.

## A note on sources

Croatia's actual dispatch systems (UVI / VATROnet, JVP Zagreb's FileMaker
database) and Carinthia's dispatch log are closed and credentialed; Styria's
live overview is fenced to Austrian addresses. This project does not attempt to
get past any of that. The open surface it does read, and everything that was
checked and rejected, is documented in `vatrocad/README.md`.
