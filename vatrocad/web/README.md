# VatroCAD · web

Static dispatcher console (Slovenian UI) for incidents in Croatia and the
Austrian border belt. Vite + TypeScript (vanilla, no framework), Leaflet with
marker clustering, supabase-js for realtime only. Deployed to GitHub Pages
under `/vatrocad/` by `.github/workflows/vatrocad-pages.yml`.

```
npm ci            # install (lockfile committed)
npm run dev       # http://localhost:5173/vatrocad/
npm test          # vitest: time-zone (DST), sorting, grouping, filters, hash
npm run build     # tsc --noEmit + vite build → dist/  (~150 KB gzip total)
npm run preview   # serve dist/ on :4173
```

## Data

* Reads `public.incidents` through PostgREST with the publishable key
  (read-only via RLS). The initial load fetches list columns only
  (`raw`/`raw_sl` are pulled on demand when a row opens), `occurred >= today − 8 d`,
  300 rows per country.
* `public.control` (id = 1) is probed every 60 s; an incremental
  `last_seen=gt.<max>` fetch runs only when `poll_finished_at` changed.
  A full reload happens every 10 minutes (catches deletes).
* Realtime `postgres_changes` events are coalesced for 1.5 s into one
  incremental fetch – never a render per event.
* `Osveži` = incremental load → `request_poll()` RPC → wait (5 s steps, ≤ 180 s)
  for `poll_finished_at` to advance → load. Throttled to once per minute.
* `public.source_status` is optional; when absent, source health is derived
  from the newest row per source.

## Fixture mode

`?fixture=1` loads `public/fixtures/incidents.json` (150 real rows captured
from one poller run, all categories/statuses, some without coordinates, some
translated) instead of Supabase. Timestamps are re-based at load so the
newest row is always "12 minutes ago". Regenerate with
`scripts/make-fixture.py` from a captured poller payload.

## Time

Everything in `src/time.ts` goes through `Intl.DateTimeFormat` with
`timeZone: 'Europe/Ljubljana'`; no fixed offsets. Wall-clock
`occurred + occurred_time` is converted to an epoch per date (DST-aware),
so the page does not depend on the poller's `ts` column being right.

## State

Filters live in the URL hash (`#c=AT&r=stmk&k=police&w=72&q=…&b=1&id=…&v=map`)
and in `localStorage` (`vatrocad.filters.v2`, minus search text and the
geolocation radius), so links are shareable and the last view survives reloads.
