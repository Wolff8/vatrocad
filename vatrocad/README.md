# VatroCAD — the poller (`vatrocad.py`) and the console (`web/`)

`vatrocad.py` is a single stdlib-only Python 3.11 script. One cycle fetches
every source due, parses each page into incident rows, fixes dates, geocodes,
translates into Slovenian, stores everything in a local SQLite file, mirrors
the changed part into Supabase, prunes the Austrian rows that have aged out,
and reports per-source health. In production it runs inside a GitHub Actions
job that stays up for 55 minutes and polls every 10 minutes
(`.github/workflows/vatrocad-poll.yml`); the console in `web/` reads Supabase
directly.

```bash
python3 vatrocad.py --once                          # one full cycle, print a summary, exit
python3 vatrocad.py --serve-minutes 55 --interval 600   # what CI runs
python3 vatrocad.py                                 # local: poll, then serve JSON on :8713
```

## Command line and environment

| Flag | Meaning |
|---|---|
| `--once` | Run one **full** cycle (every source, validators skipped, every row re-pushed), print the per-source table and the cycle timing, exit. |
| `--serve-minutes N` | CI mode (`serve_loop`): stay up for N minutes, poll on the interval, honour `control.poll_requested_at`, stamp `control`, then exit. The interval floor here is 300 s. |
| `--interval SECONDS` | Seconds between polls (default 600). Floor 300 s in `--serve-minutes` mode, 60 s in local server mode. These are small volunteer-brigade servers; stay at 10 minutes. |
| `--port PORT` / `--host HOST` | Local server mode only (default `127.0.0.1:8713`). No authentication — do not bind `0.0.0.0` on a network you do not trust. |

Local server mode (no flags) does an initial full cycle, then polls in a
background thread and serves `GET /api/incidents` (rows from the last 3 days
plus summary/weather context and per-source freshness, JSON) and
`GET /api/refresh` (forces a cycle, returns `{"new": n}`). It expects a
`dashboard.html` next to the script for `/`; that file no longer exists — the
console is the Vite app in `web/`, which reads Supabase, not this server. Use
local mode for `/api/incidents` and for developing parsers.

| Variable | Used for |
|---|---|
| `SUPABASE_URL` | Project URL. With `SUPABASE_SERVICE_KEY` set, every cycle pushes to Supabase; without both, the poller is SQLite-only and every Supabase step reports `off`. |
| `SUPABASE_SERVICE_KEY` | The **service_role** key (bypasses RLS to write). CI secret only — never in the console. |
| `NOMINATIM_BUDGET` | Nominatim lookups allowed per cycle, shared by Croatian street refinement and Austrian towns (default 25; the workflow sets 15 on a cold cache). |

Every run needs write access to `vatrocad.sqlite3` next to the script; the
schema is created and migrated in place (`db()`).

## Sources

50 entries in `SOURCES`, 34 in the FAST tier and 16 in the SLOW tier
(`SLOW_SOURCES`). FAST sources run every cycle, SLOW ones every second cycle;
a manual request and the first cycle of a job always run everything. Each
entry is `(label, fetch function, method)`; the label is what lands in
`incidents.source` and `source_status.source`, and **the console classifies
rows by that label and by the `ref` prefix** (see *Conventions* below).

### Croatia

**Brigade, JVP and DVD intervention logs** — the brigades' own per-call pages.

| Source | Region | What it provides / how it is read | Tier |
|---|---|---|---|
| `DVD Vratišinec` | `med` | WordPress REST API, 50 newest posts; only posts titled `… (N/YYYY)` are calls, the number becomes `ref`; time from `HH:MM sati`; a street that will not geocode falls back to the village centroid. | FAST |
| `JVP Šibenik` | `sib` | Paginated HTML archive (`/index.php/intervencije`, two pages of 30); blocks `DD.MM.YYYY.` + `U HH:MM zaprimljena je dojava …`; crew and vehicle counts parsed from the text; headline is the first clause, normalised. | FAST |
| `DVD Supetar · intervencije`, `DVD Garčin · intervencije`, `DVD Valpovo · intervencije`, `JVP Osijek · intervencije` | `sdz`, `bpz`, `obz`, `obz` | Category RSS feeds read by the shared newsroom parser (JVP Osijek's category feed is disabled, `?cat=3&feed=rss2` works). A `Datum: DD.MM.YYYY.` in the body dates batch-posted calls. Found by scanning `dvd-`/`jvp-`/`vatrogasci-` domains for every Croatian town. | FAST |
| `DVD Horvati` | `zag` | WordPress REST API, "Intervencije" category resolved by name; a few posts a year, but the only open Zagreb brigade log. | SLOW |
| `VZ Međimurske ž.` | `med` | Classic WordPress feed `?cat=3&feed=rss2` (`wp-json` is disabled); notable calls plus tallies — `54 intervencije …` becomes a `summary` row with `crew` = the count. | SLOW |

**County 112 centre**

| Source | Region | What it provides / how it is read | Tier |
|---|---|---|---|
| `ŽVOC Šibenik-Knin` | `sib` | HTML bulletins `Priopćenje o vatrogasnim događajima DD.MM.YYYY, HH:MM`: the twice-daily digest (bulleted items, each its own row, time from `dojava … HH:MM`) and the unbulleted "flash" bulletin posted hourly during a large fire (one row, `ref ŽV-…-F`, status read from the text: `na terenu` / `u tijeku` → active, `lokaliziran` → contained, `ugašen` → closed). | FAST |

**Police**

| Source | Region | What it provides / how it is read | Tier |
|---|---|---|---|
| `Policija · 20 PU` | one code per county (`POLICE_PUS`) | All 20 county police administrations on the shared gov.hr CMS, `https://<slug>-policija.gov.hr/vijesti/8`, one parser. Headlines in `POLICE_SKIP` (PR, arrests, thefts…) are dropped unless the headline is itself a crash/fire/rescue; only `accident`, `fire`, `rescue` and genuine tallies (`summary`) are kept. Article bodies come from the article cache (at most 6 new per PU per cycle); the event date is read from prose (`U petak, 4. rujna oko 3:20`), the time from `oko HH:MM` / `u HH:MM sati`; brigade units and `ZHM` (ambulance named) go into `units`; listings older than 4 days are ignored. Runs as 20 concurrent tasks; every PU also gets its own `source_status` row (`PU zagrebačka`, …). | SLOW |
| `MUP · nacionalno` | `nat` | `policija.gov.hr/vijesti/8`, the "Sažetak prometnih nesreća" articles: one `summary` row per bulletin with accidents-with-casualties, killed, injured, serious (`raw` is JSON), dated to the end of the reporting window. | FAST |

**HGSS, HAC, Meteoalarm, HVZ**

| Source | Region | What it provides / how it is read | Tier |
|---|---|---|---|
| `HGSS · spašavanje` | `nat` | Mountain-rescue RSS (`hgss.hr/feed/`); training/ceremony posts (`HGSS_SKIP`) dropped, only search/rescue vocabulary kept; category `rescue`; geocoded to the massif named, else the responding station's town from the RSS `<category>`. | SLOW |
| `HAC · autoceste` | `nat` | The motorway operator's "stanje na autocestama" page, one `Izvanredni događaj` block per stretch, category `tech`, last 3 days. Low frequency, no overlap with anything else. | SLOW |
| `Meteoalarm` | `nat` | CAP 1.2 Atom feed for Croatia; lapsed warnings dropped; category `weather`, `units` = severity, onset converted to local time. Context for the console, never an incident. | SLOW |
| `HVZ · DVOC 193` | `nat` | `hvz.gov.hr/vijesti/8`, the newest three DVOC nightly digests → one `summary` row each (interventions, organisations, firefighters, vehicles, aircraft, hectares in `raw` as JSON), dated to the second day of the `za dane 06./07. …` window at 07:00. | FAST |

**Newsrooms** — the fastest layer for counties whose brigades do not
self-publish. The 13 regional and 5 national RSS feeds below, together with
the four brigade category feeds listed above, are one table (`NEWSROOMS`, 22
rows) read by one parser (`make_newsroom`); sibenik.in has its own HTML
parser. In `make_newsroom`, crime stories (`blotter_skip`) are dropped, the category must
be `fire`/`accident`/`tech`/`ems`/`rescue`, `pubDate` is parsed with its own
offset, an hour named in the story (`oko 21:40`, `Vrijeme dojave:`) beats the
publication hour and is dated to the previous day when it is later than the
publication time (`event_when`).

| Source | Region | Notes | Tier |
|---|---|---|---|
| `eMeđimurje · kronika` | `med` | headline opens with the settlement in capitals (`caps_lead`), which pins the location | FAST |
| `kaportal · kronika` | `kaz` | | FAST |
| `Dalmacija danas · kronika` | `sdz` | | FAST |
| `sisak.info · kronika` | `smz` | | FAST |
| `Zadarski list · kronika` | `zdz` | | FAST |
| `Podravski · kronika` | `kkz` | | FAST |
| `Varaždinski · kronika`, `Vzaktualno · kronika` | `vaz` | | FAST |
| `Međimurski · kronika` | `med` | | FAST |
| `Požeški · kronika` | `psz` | | FAST |
| `Bjelovar.live · kronika` | `bbz` | | FAST |
| `Zagorje.com · kronika` | `kzz` | the site's own `/rss` (the radio station's crna-kronika category is a dead 2014 archive) | FAST |
| `ICV · kronika` | `vpz` | | FAST |
| `24sata · vijesti`, `Index · vijesti`, `Jutarnji · vijesti`, `Večernji · vijesti`, `tportal · vijesti` | placed by the settlement named (`region=None` → nearest police-administration seat via `region_for`) | National feeds carry everything, so the **headline itself** must name an incident, and an item naming no known place is dropped. The only afternoon coverage of Zagreb. | FAST |
| `sibenik.in · kronika` | `sib` | Not RSS: the HTML listing, then up to 12 articles (6 new fetches per cycle). The only real clock is the byline in the body, `DD.MM.YYYY @ HH:MM` — the sitemap `<lastmod>` is a rebuild stamp — and an article whose date cannot be read is dropped, never guessed. Carries the ŽVOC bulletins with more narrative plus incidents they skip. | FAST |

### Austria

| Source | Region | What it provides / how it is read | Tier |
|---|---|---|---|
| `NÖ Feuerwehr · Wastl` | `noe` | Lower Austria's statewide dispatch log on `feuerwehr-krems.at`: `Land_EinsatzHistorie.asp` (closed calls, second-precision timestamp, `ref NOE-MMDD-HHMMSS`, status `closed`) and `Land_EinsatzAktuell.asp` (running calls with only a date and `~ N std.`; emitted as `ref NOE-LIVE-MMDD`, status `active`, the first estimated start persisted in `live_start` so it does not drift). Fire watches (`Brandsicherheitswache`, `SOF0`) stay off the live list. Type text glossed to Slovenian by `AT_SL_GLOSSARY`; category from the code/type (`AT_CATCODE`); exercises (`U…`) kept as `exercise`; last 3 days; town geocoded via Nominatim. | FAST |
| `OÖ Feuerwehr · LFV` | `ooe` | Upper Austria's `einsaetze.ooelfv.at/einsatz/2tage`, fetched by POST with `exercise=Y` so the unconfirmed "Einsatz od. Einsatzübung" rows appear. Each responding brigade has an alert time and, once released, an end time: the call's time is the earliest alert, it is `closed` only when every brigade has an end time, `units` lists the brigades and `vehicles` counts them. Grey rows without a recognisable type are exercises. | FAST |
| `LFV Štajerska · poročila` | `stmk` | Styria's LFV `Einsaetze-Berichte.aspx`, the statewide list of brigade reports. Each report page is fetched once ever (article cache, at most 10 new per cycle) for the alarm time (`um/gegen HH:MM Uhr`) and the town; a row without a page yet is completed on a later cycle. Last 7 days. | SLOW |
| `BFKDO Klagenfurt-Land · poročila`, `BFKDO Villach-Land · poročila`, `BFKDO Wolfsberg · poročila`, `BFK Völkermarkt · poročila`, `FF Völkermarkt · poročila`, `FF Lavamünd · poročila` | `ktn` | District-command and brigade WordPress/Jimdo RSS (`AT_FEEDS`), one parser (`make_at_feed`): competitions, anniversaries, blessings, courses (`AT_REPORT_SKIP`) dropped, `Übung` kept and tagged; category from narrative German (`AT_REPORT_CATCODE`); alarm date from `Am DD.MM.YYYY` or a spelled-out month, time from `um/gegen HH:MM Uhr`, else the publication stamp; town from `(Gemeinde X)` / `in X`, else the district seat's coordinates; `units` = every `FF/BF/Feuerwehr X` named. Last 7 days, status `closed`. | SLOW |
| `FF Mureck · poročila`, `FF Bad Radkersburg · poročila`, `FF Feldbach · poročila` | `stmk` | Same parser and rules as above. | SLOW |
| `Policija Štajerska · LPD`, `Policija Koroška · LPD`, `Policija Sp. Avstrija · LPD`, `Policija Zg. Avstrija · LPD` | `stmk`, `ktn`, `noe`, `ooe` | The Interior Ministry's per-state press RSS (`bmi.gv.at/rss/<stmk|ktn|noe|ooe>_presse.xml`, `AT_POLICE`): several a day, per incident. Pure crime (`AT_POLICE_SKIP`: fraud, burglary, arrests, investigations…) dropped; category from `AT_POLICE_CATCODE` (accident, fire, rescue, ems). Styria opens with `District \| Town. –`; otherwise `Gemeindegebiet von X`, the title, `Bezirk X`. `units` = `Polizei` plus every helper named (Feuerwehr X, Christophorus N, Rotes Kreuz, Bergrettung, Notarzt, Alpinpolizei). Last 7 days, status `closed`. | FAST |

Not reachable and therefore not polled: Styria's live overview
(`einsatzuebersicht.lfv.steiermark.at`, no answer from outside Austria —
checked from the sandbox, from GitHub's runners with `probe.yml`, and from a
third fetcher) and Carinthia's `feuerwehr.einsatz.or.at` (login only). No
Austrian EMS or helicopter feed is public; EMS-adjacent fire calls
(`Tragehilfe`, `Hubschrauberlandeplatz`, `Notarzt`) are tagged `ems` instead.

## One cycle (`poll_once`)

1. **Reset budgets**, load the HTTP validators into memory, evict stale cache
   rows (`evict_caches`).
2. **Fetch** every source due this cycle concurrently (`fetch_sources`):
   `MAX_WORKERS = 6` threads, heaviest groups first (the 20 police PUs, NÖ, OÖ,
   LFV Steiermark, sibenik.in), never two requests to one host at once
   (`_host_lock`). `fetch()` sends `If-None-Match` / `If-Modified-Since` from
   the validators stored in SQLite (skipped on a resync cycle), retries a
   network failure once after 2 s, and returns `(text, status)` with status
   `ok`, `unchanged` (304) or `error: …`. Every parser returns
   `(rows, status)` and never raises — a redesigned site degrades to zero rows
   and a red source, nothing else breaks.
3. **Date guard** (`defuture`): an incident stamped more than 5 minutes in the
   future is a parse bug (a body hour paired with the wrong day) and is moved
   back one day; weather warnings are legitimately future-dated and left alone.
4. **Street refinement** (`refine_locations`): where a Croatian text names a
   street (`STREET_RE`), Nominatim sharpens the settlement centroid to the
   street, but only if the hit is within ~0.35° lat / 0.5° lon of the centroid.
5. **Translation** (`translate_rows`, below).
6. **Clustering** (`assign_clusters`): Croatian rows with the same day,
   category, ~2 km cell and 45-minute bucket from at least two *different*
   sources get one `cluster` key. Written to SQLite and Supabase; the console
   does not fold on it yet.
7. **Store** (`store`): insert new rows with `first_seen`; update known rows
   whose content changed (a corrected date, a closed status, a sharper fix, a
   translation), bumping `last_seen`; a translation missing from this parse
   never wipes a stored one. Then `_record_source` writes the local `sources`
   table.
8. **Push** (`push_supabase`, below) and, if the push went through, **prune**
   Austria (`prune_supabase_austria`), then **`push_source_status`**.

The log line at the end reads
`cikel: 41.2 s (viri 30.1 s, ulice 0.0 s, prevod 4.8 s, shranjevanje 0.9 s, supabase 5.4 s), 812 vrstic, 7 novih, 3 spremenjenih`.

## CI cadence (`serve_loop`)

GitHub's cron is best-effort and dropped most `*/15` slots on this repository,
so scheduling lives in the job itself:

- The workflow runs `python3 vatrocad.py --serve-minutes 55 --interval 600`.
  `serve_loop` polls immediately (cycle 0: everything, with `resync=True`),
  then every 600 s. `full = manual or first or cycle % 2 == 0`: FAST sources
  every cycle, SLOW sources every second cycle.
- Every 15 s it reads `public.control`. A `poll_requested_at` it has not yet
  acted on triggers a full cycle at once, provided the last poll is more than
  45 s old — that is how the console's `Osveži` button is served within ~15 s.
  After two consecutive failed control reads the loop backs off to 60 s.
- Around each cycle it stamps `control`: `poll_started_at`, `runner_until`
  (the job's deadline) and `note = "pobiram s virov…"` before; afterwards
  `poll_finished_at` and `note = "N novih"` **only if the push succeeded** —
  otherwise `note = "Supabase ne odgovarja – podatki niso osveženi"` and
  `poll_finished_at` stays put, because the console's freshness comes from it.
  On exit: `note = "pobiralnik miruje – naslednji zagon ob polni uri"`,
  `runner_until = null`.
- The workflow's last step queues the next run (`gh workflow run`, legal with
  `GITHUB_TOKEN` for `workflow_dispatch`); with `cancel-in-progress: false` it
  starts the moment this one exits. Guards: only after the poll step actually
  ran, never before the job is 5 minutes old, and not when a successor is
  already queued. The single `3 * * * *` cron merely restarts the chain if a
  runner dies. Details and how to stop it: `DEPLOY.md`.

## Delta push and retention (`push_supabase`, `prune_supabase_austria`)

- `sb_payload` builds the Supabase row from `SB_COLUMNS` (`id, source, region,
  country, ref, occurred, occurred_time, ts, category, status, title,
  location, lat, lon, units, crew, vehicles, raw, link, title_sl, raw_sl,
  cluster`) plus `last_seen = now`. `status` is the parser's own where it has
  one (Austrian dispatch, ŽVOC flash) and otherwise derived from Croatian verbs
  (`status_of`: `u tijeku` → active, `lokaliziran` → contained, `ugašen` →
  closed, else unknown). `first_seen` is a database default.
- A content hash (`content_hash`, everything except `last_seen`) is stored per
  row in SQLite. A row goes over only when it is new, its hash changed, it is
  an NÖ running call (`NOE-LIVE`, whose `last_seen` drives the live prune), or
  the cycle is a **resync** (the first cycle of every job re-pushes everything
  ≤ 8 days old, so a lost write heals itself).
- Rows with `occurred` older than `SB_PUSH_MAX_AGE_DAYS = 8` are never pushed;
  the console loads eight days and the Croatian archive lives in SQLite.
- Rows are deduplicated by id first (PostgREST turns the batch into one
  `INSERT … ON CONFLICT`, and a duplicate id would reject the whole batch),
  then sent in chunks of 200 with `resolution=merge-duplicates`. A chunk the
  server rejects (4xx) is bisected so only the malformed row is lost. If the
  table predates the `cluster` column the poller drops it and carries on.
- Retention:

| Rows | SQLite | Pushed | Deleted from Supabase |
|---|---|---|---|
| Croatia (all sources) | kept for good | if `occurred` ≥ today − 8 d | never |
| Austria, dispatch (`NÖ Feuerwehr · Wastl`, `OÖ Feuerwehr · LFV`) | kept | ≤ 8 d | `occurred` < today − 3 d (`AT_MAX_AGE_DAYS`); an NÖ `NOE-LIVE` row not re-seen for 40 minutes (the call closed and its exact-time history row replaced it) |
| Austria, reports and police | kept | ≤ 8 d | `occurred` < today − 7 d (`AT_REPORT_MAX_AGE_DAYS`) |

  Pruning runs after a successful push, never before, so nothing is deleted
  that has not landed.

## When Supabase does not answer

`sb_call` is the one door to Supabase: 15 s timeout (10 s for `control`, 20 s
for an upsert chunk), two retries with 2 s / 5 s back-off, 4xx answers final (a bad request is not an
outage), 5xx/timeouts/network errors trip a **per-cycle circuit breaker**.
Once tripped, every further Supabase call in that cycle returns instantly, one
line is logged (`Supabase ne odgovarja (…) — nadaljujem brez sinhronizacije v
tem ciklu`), scraping and SQLite storage continue, `poll_finished_at` is not
stamped and `control.note` says why (if `control` itself is reachable).
Because pushed state is tracked by hash, the first cycle that gets through
sends every new and changed row, and the next job's first cycle re-pushes the
whole 8-day window regardless. No manual catch-up is ever needed; restarting
the Supabase project is enough (`DEPLOY.md`).

## `source_status`

After every cycle `push_source_status` upserts one row per source polled this
cycle into `public.source_status` (`on_conflict=source`): `source`, `ok`
(status was `ok` or `unchanged`), `rows` (rows parsed this cycle), `ms`,
`error` (the status text when not ok, ≤ 200 chars), `checked_at`. The 20
police PUs are reported individually as well as under `Policija · 20 PU`, so
a full cycle writes 70 rows. The table's `country` column is not written; the
console derives the country from the source name. If the table does not exist
the poller logs `tabela source_status ne obstaja` and carries on — the console
then derives health from the newest row per source instead.

## Caches (the SQLite file)

`vatrocad.sqlite3` is both archive and cache, and the workflow restores it
from the Actions cache before each job and saves it afterwards (even when the
poll step fails). Tables:

| Table | Holds | Eviction |
|---|---|---|
| `incidents` | every row ever parsed, with `hash`, `first_seen`, `last_seen` | never |
| `sources` | local per-source status/count, used by `/api/incidents` | overwritten each cycle |
| `http_validators` | ETag / Last-Modified per URL | 30 days |
| `article_cache` | reduced text of article/report pages, keyed by URL (articles are immutable) | 30 days, and capped at 4000 rows |
| `geocode_cache` | Nominatim results, keys `street\|town` (HR) and `AT\|town\|state` (AT) | hits kept for good; misses retried after 24 h |
| `translate_cache` | MyMemory results keyed by `de\|md5` / `hr\|md5` of the source text | never |
| `meta` | `mt_words_YYYYMMDD` daily word counters and one-shot migration flags | never |
| `live_start` | first estimated start of each NÖ running call | 3 days |

Per-cycle budgets stop a cold cache from stalling a poll: article fetches per
source (`fetch_article` — police 6 per PU, MUP 4, sibenik.in 6, DVOC 3, LFV
Steiermark 10) and Nominatim lookups (`NOMINATIM_BUDGET`, default 25, one
request every 1.1 s with an identifying User-Agent, per Nominatim's policy).
Article fetches and geocode lookups run from the fetch threads, each with its
own SQLite connection (`cache_db`).

## Geocoding

Croatian text is placed with the static gazetteer `PLACES` (about 300
settlements, county seats, Zagreb districts, massifs for HGSS): first a
location phrase (`na području X`, `u naselju X`, `kod X`, `u X`), then the
longest gazetteer name appearing as a whole word, with unit names
(`JVP/DVD/VZ/HGSS X`) scrubbed first so a brigade's home town is not mistaken
for the scene. Declension is handled by stem comparison (`Pirovca` → Pirovac,
`Šibeniku` → Šibenik; names of five letters or fewer must match exactly, plus a
case ending). Add places as `"name": (lat, lon)`; an unknown place still shows
in the log, only without a marker. The result is a settlement centroid; the
Nominatim street refinement above is the only finer fix.

Austrian towns are geocoded with Nominatim (`countrycodes=at`, first `town,
state, Österreich`, then `town, Österreich`) and cached for good, so NÖ's
recurring towns cost one lookup each, ever.

## Time

All wall-clock time is `LOCAL = Europe/Zagreb` (with a hand-written EU DST rule
as fallback for a runner without tzdata); `to_ts` turns `date + HH:MM` into the
`ts` column. A row without a time is treated as 00:00 (ages out sooner, never
looks fresher). RSS `pubDate` offsets are honoured (`_parse_pubdate`); a body
time later than the publication time is dated to the previous day
(`event_when`) — a report cannot describe something that has not happened yet.

## Slovenian translation (`translate_rows`)

The console is Slovenian; the feeds are Croatian and German.

- Austrian **dispatch** rows (`ref` starting `NOE-`/`OOE-`) never touch the
  network: `title` is already the glossed type (`at_translate` over
  `AT_SL_GLOSSARY`, 124 phrases observed in the live feeds, longest first,
  short generic words word-bound), and `raw_sl` is the same gloss of the type.
- Every other row is translated once with **MyMemory**'s keyless endpoint
  (`langpair=de|sl` or `hr|sl`) into `title_sl` and `raw_sl`, and cached in
  `translate_cache` for good. Only rows inside the display window are
  translated: dated within the last 4 days, 8 for `stmk`/`ktn`. `summary` and
  `weather` rows are skipped.
- Budgets: `MT_DAILY_WORDS = 4000` per UTC day (the anonymous tier allows
  about 5000 per IP; counted in `meta`), `MT_CALLS_PER_CYCLE = 30`, 0.4 s
  between calls. Narratives go over in sentence-aligned chunks of at most 450
  bytes (MyMemory's limit is 500 bytes), at most 4 chunks; only a complete
  translation is cached. Order: newest first, and within a day the few Austrian
  border reports before the large Croatian set, so the belt is never starved.
- A 429/503, a quota message or a network error stops translation for the
  cycle; the rest retries next time. Until then the console shows the clean
  original — never a half-translated title.

## Categories, kinds, status

`category` is one of `fire`, `tech`, `accident`, `rescue`, `ems`, `weather`,
`exercise`, `summary`, `other`. Croatian text is classified by
`CATEGORY_RULES` (headline first, body second; a tally headline → `summary`,
`is_roundup`); Austrian dispatch by `AT_CATCODE`, reports by
`AT_REPORT_CATCODE`, police by `AT_POLICE_CATCODE`.

The console derives a **kind** from the row (`web/src/classify.ts`):
`weather` for weather rows; `police` when the source matches
`^(Policija|PU |MUP)`; `report` for `summary` rows, for Austrian rows whose
`ref` does not start with `NOE-`/`OOE-`, and for Croatian sources ending in
`· kronika` / `· vijesti` or starting `HVZ ·`; everything else is `dispatch`.

## Adding a source

1. **Inspect it** from a GitHub runner if the sandbox cannot reach it:
   Actions → *probe URL*.
2. **A WordPress/RSS feed** is one row, not a function: a Croatian crna-kronika
   or brigade category feed goes into `NEWSROOMS` (`label, region, url,
   fallback place key, id prefix, caps_lead`), an Austrian brigade/district
   feed into `AT_FEEDS` (`label, region, state, url, ref prefix, fallback
   (lat, lon)`), another Landespolizeidirektion into `AT_POLICE`. Anything
   else is a `src_*()` function returning `(rows, status)` — use `fetch()`
   (conditional GET) or `fetch_post()`, `fetch_article()` for bodies, and
   never raise.
3. **Row dict:** `id` (stable; `rowid(source, date, time, body)` or a
   `prefix:` + the site's own id), `source` (the label), `region` (a PU code,
   `nat`, or `noe|ooe|stmk|ktn`), `country` (`AT`, or omit for `HR`), `ref`,
   `date` (`YYYY-MM-DD`), `time` (`HH:MM` or `""`), `category`, optional
   `status`, `title`, `location`, `lat`/`lon` (or `None`), `units`, `crew`,
   `vehicles`, `raw` (≤ 1600 chars), `link`. Bound the parse with a cutoff
   (3 days for dispatch-like pages, 7 for Austrian reports) so the archive is
   not re-parsed every cycle.
4. **Register** it in `SOURCES` as `(label, function, method)`; add the label
   to `SLOW_SOURCES` if it publishes a few times a day. Mind the naming
   conventions above — a label ending `· kronika` is shown as a report, a
   `Policija …` label as police, an Austrian `ref` outside `NOE-`/`OOE-` as a
   report.
5. **Places:** add any settlement the new source names to `PLACES`.
6. **Test:** `python3 vatrocad.py --once` and read the per-source line
   (`rows, novih, sprem., ms`, plus `[n predatiranih]` if the date guard had to
   move rows — a sign the parser pairs hours with the wrong day). Then let CI
   run and check `Viri` in the console.

Be a good neighbour: several of these are small volunteer sites; conditional
GET and the per-host lock keep a cycle to one polite client per server.

## What was checked and rejected

Kept short, so it is not repeated:

- **Croatia's dispatch systems** (UVI / VATROnet, run by HVZ under NN 80/2021)
  have no public endpoint. JVP Zagreb's per-call data is a credentialed
  FileMaker database (`fm.vatrogasci-zagreb.hr`, Data API answers 401); the
  HVZ shared CMS for hundreds of DVDs (`*.spis.hvz.hr`) does not resolve from
  outside. Ask for access if you have standing — it beats every scraper here.
- **All 21 county EMS institutes** publish reports and hiring notices, no
  interventions. The police PUs are the only nationwide per-incident feed.
- **Brigade sites:** ~9,400 candidate hosts scanned; 133 exist, six keep a
  per-call log alive in 2026 (all included). Nineteen stopped (JVP Drniš 2018,
  Vodice 2021, Pula 2025, Čakovec and Samobor early 2026, JVP Požega 2019…).
- **Facebook**, where most volunteer brigades actually post: not open, not
  stable, deliberately not scraped.
- **Newsrooms rejected:** prigorski.hr (aggregates national stories under a
  Zagreb region), mnovine.hr (403), medjimurje.info and tris.com.hr (no
  incident category), RHZK crna kronika (dead since 2014), sjeverozapad.hr
  (`/feed/` is a comments feed), eVaraždin and Varaždinske vijesti (no feed),
  antenazadar.hr (editorial only).
- **Austria:** Notruf NÖ 144 (login), Leitstelle Tirol (aggregate statistics),
  Red Cross (nothing machine-readable), commercial kiosk products, a
  since-shut-down Christophorus tracker. HAK's traffic map is a JS app with no
  data endpoint; HAC's server-rendered page is used instead.
- **Registries:** data.gov.hr (registries and PDFs), Postman, SwaggerHub, MCP
  directories — no Croatian emergency API exists.

## Frontend (`vatrocad/web`)

Static console, Slovenian UI, Vite 6 + TypeScript with no framework; Leaflet
with marker clustering on Esri dark-grey tiles; `@supabase/supabase-js` is
loaded lazily and used only for the realtime channel — all reads are plain
PostgREST `fetch` calls with the publishable key. Built and deployed by
`.github/workflows/vatrocad-pages.yml` (Node 20: `npm ci`, `npm test`,
`npm run build`, deploy `dist/`) to <https://wolff8.github.io/vatrocad/>.
`web/README.md` is the short reference; this is the longer tour.

```bash
cd vatrocad/web
npm ci             # install (lockfile committed)
npm run dev        # http://localhost:5173/vatrocad/  (base path is /vatrocad/ everywhere)
npm test           # vitest: time zone/DST, classification, sorting, grouping, filters, hash, fixture
npm run build      # tsc --noEmit + vite build → dist/  (~146 KB gzipped: app + leaflet chunk + css)
npm run preview    # serve dist/ on :4173
```

**Fixture mode.** Append `?fixture=1` (works on the dev server and on the
deployed page): `public/fixtures/incidents.json` — 150 real rows from one
poller run covering every country/category/status, every source, translated
and untranslated rows, rows without coordinates — is loaded instead of
Supabase, with all timestamps re-based so the newest row is always 12 minutes
old. Realtime, the control head-check and `Osveži`'s poll request are off; the
header reads `demo podatki`. Regenerate with `scripts/make-fixture.py
<captured payload> public/fixtures/incidents.json`.

**What the page does**

- Loads list columns only (`LIST_COLS`; `raw`/`raw_sl` are fetched when a row
  is opened), `occurred ≥ today − 8 d`, 300 rows per country, ordered by `ts`.
- Every 60 s it reads `control` (one tiny request) and runs an incremental
  `last_seen > max seen` fetch only when `poll_finished_at` changed; every 10
  min a full reload catches deletes. Realtime `postgres_changes` events are
  coalesced for 1.5 s into one incremental fetch (deletes are applied
  directly). Loads pause while the tab is hidden and resume on
  `visibilitychange`/`online`.
- Header LED: `v živo` / `osveževanje` / `ni povezave` / `povezujem…`, then
  `pobrano pred N min` from `control.poll_finished_at`, then `pobiralnik teče`
  or `pobiralnik miruje` from `control.runner_until`.
- `Osveži`: incremental load → `rpc/request_poll` → toast `Pobiram s virov…`
  → poll `control` every 5 s for up to 180 s until `poll_finished_at`
  advances → incremental load → `Sveže s virov · ni novega` or `+N novih ·
  skupaj M`. If no poller is up: `Pobiralnik trenutno ne teče – podatki iz
  baze`; on timeout: `Pobiralnik se ne odziva · prikazani so zadnji shranjeni
  podatki`; throttled to once a minute (`Počakaj minuto pred naslednjo
  osvežitvijo`).
- Country tabs `Vse / Avstrija / Hrvaška` with live counts; quick rail with the
  time window (`1 h … 7 d`, default 24 h), `Aktivno`, and the kind chips
  (`Dispečer`, `Poročila`, `Policija`, `Vreme`); the `Filtri` sheet adds
  status, category, region, `Samo obmejni pas (Štajerska, Koroška)` and
  `V bližini` (25/50/100 km from the browser's geolocation). Every chip shows
  how many rows it would yield. Filters live in the URL hash
  (`#c=AT&r=stmk&k=police&cat=fire&s=active&w=72&q=…&b=1&d=50&v=map&id=…`) and
  in `localStorage` (`vatrocad.filters.v2`, without the search text and the
  radius), so links are shareable and the last view survives a reload.
- List: active rows first, then newest; grouped `Zadnja ura` / `Danes` /
  `Včeraj` / `Starejše` / `Prihajajoče` / `Vremenska opozorila` (active rows
  are pulled into the top group); `NOVO` for 30 minutes after `first_seen`;
  keyboard navigation; 300 rows then `Prikaži več`. Detail view: dispatch-style
  headline (`POŽAR · Požar stanovanjske hiše · Leibnitz`), key/values, the
  alarm code explained for NÖ (`B3` = kind + level), Slovenian narrative with
  the original one tap away (`Izvirnik (nemško)`) and a `samodejni prevod`
  note, unit chips, `Pokaži na zemljevidu`, `Deli` (Web Share or clipboard),
  `Odpri vir`. On phones everything is a bottom sheet (drag to close, Back
  closes it); on desktop the map sits beside the list.
- Stats strip: `N prikazanih · N aktivnih · N AT · N HR`, a 24-hour histogram,
  and `Viri` → the source-health sheet (`Viri podatkov`): `NAPAKA` (poller
  reported an error), `V ŽIVO` (newest event ≤ 12 h), `NEDAVNO` (≤ 3 d),
  `STARO`, from `source_status` when present, else from the newest row held.
- Notifications: opt-in bell; at most one in-tab `Notification` per batch for
  new **active** fire/accident/rescue rows first seen in the last 30 minutes.
- PWA: `manifest.webmanifest`, icons, and `sw.js` generated at build time by
  the plugin in `vite.config.ts` — app shell only (network-first document,
  cache-first hashed assets); `/rest/`, `/realtime/`, `/fixtures/` and map
  tiles are never cached. Registered only in production builds.

**`src/` layout**

| File | Role |
|---|---|
| `main.ts` | boot, state, data flow (full/incremental loads, head-check, realtime, `Osveži`), event wiring for rail, sheets, map, keyboard, hash |
| `config.ts` | Supabase URL + publishable key, `LIST_CAP`, `LOAD_DAYS`, all timers, `LIST_COLS`, `FIXTURE_MODE`, `BASE` |
| `data.ts` | PostgREST calls (`fetchInitial` per country, `fetchIncremental`, `fetchRaw`, `readControl`, `requestPoll`, `fetchSourceStatus`), fixture loading and re-basing |
| `store.ts` | keyed in-memory row set emitting one `ChangeSet` per batch; tracks the newest `last_seen` |
| `classify.ts` | `mapRow`: enrich a row once (category/status normalisation, kind, epoch, `active`, helicopter flag, folded search haystack); display titles with the DE/HR gloss fallback, Meteoalarm titles, alarm-code explanation, safe links |
| `filters.ts` | the `Filters` shape, hash parse/serialise, matching, sort, band grouping, facet counts, hour histogram |
| `time.ts` | everything wall-clock, through `Intl` with `Europe/Ljubljana` (DST-safe); formatting; bands |
| `labels.ts` | every Slovenian UI string (`T`), category/kind/status labels and colours, region names, windows, radii, NÖ code tables |
| `notify.ts`, `geo.ts`, `types.ts` | notifications, haversine, row/control/source-status types |
| `ui/dom.ts` | `$`, `esc`, `toast`, `debounce`, `isPhone` (< 1024 px) |
| `ui/sheet.ts` | bottom sheet / side drawer with history entry, Escape, drag, focus management |
| `ui/list.ts` | keyed reconciling list (rows reused, only changed rows re-rendered), roving tabindex, periodic relative-time tick |
| `ui/map.ts` | Leaflet map, cluster group, category-coloured pins, popups, fly-to |
| `ui/detail.ts`, `ui/filtersUi.ts`, `ui/stats.ts`, `ui/sprite.ts` | detail sheet and share text, rail + filter panel, stats strip + source health, inline SVG icons |
| `styles.css` | the whole stylesheet (dark theme) |
| `*.test.ts` | vitest, Node environment |

Also in `web/`: `index.html` (the shell), `public/` (manifest, icons,
fixture), `scripts/make-fixture.py`, `vite.config.ts` (base `/vatrocad/`,
the service-worker plugin, a separate `leaflet` chunk), `tsconfig.json`
(strict, `noUncheckedIndexedAccess`).
