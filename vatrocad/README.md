# VatroCAD

A dispatch-style console for Croatian fire-service interventions that runs entirely
on your own machine.

No API keys. No third-party packages. No cloud service, mine or anyone's — the only
network traffic is your computer asking the brigade websites for their public pages.

```bash
python3 vatrocad.py            # → http://127.0.0.1:8713
```

Python 3.9+ and nothing else. Two files do the work: `vatrocad.py` (fetch, parse,
store, serve) and `dashboard.html` (the console).

## What it does

Every 10 minutes it polls the handful of Croatian sources that publish fire
interventions, parses them into structured records, and stores them in SQLite.
The dashboard reads from your local database, so it stays useful even when a
source is down.

**The database is the point.** Most of these sites show only a rolling window —
JVP Šibenik keeps about ten calls, the county bulletins a few days. Leave this
running and you accumulate an archive none of them retain.

## Sources

| Source | Area | Resolution | How it's fetched |
|---|---|---|---|
| **Policija · 20 PU** | every county | per incident, daily | gov.hr CMS, one parser for all 20 |
| **MUP · nacionalno** | national | casualty totals, every few days | HTML summary article |
| **eMeđimurje · kronika** | Međimurje | per incident, within hours | media · WP category RSS |
| **sibenik.in · kronika** | Šibenik-Knin | per incident + ŽVOC bulletins | media · HTML + byline clock |
| DVD Vratišinec | Međimurje | every call, numbered | WordPress REST API (JSON) |
| VZ Međimurske županije | Međimurje county | notable calls + tallies | WP classic RSS, `?cat=3` |
| DVD Horvati | Zagreb | notable calls only | WordPress REST API (JSON) |
| JVP Šibenik | Šibenik city | every call | HTML log, dated blocks |
| ŽVOC Šibenik-Knin | county | 2× daily bulletin | HTML, bulleted events |
| HVZ · DVOC 193 | national | nightly digest | HTML listing → article |
| Meteoalarm | national | weather warnings | CAP 1.2 Atom feed |

A first run stores roughly 100 incidents, of which 30–40 fall inside the live window.

## Two windows: 12 hours live, 3 days maximum

The console has two bands and a hard ceiling:

- **Live · 12h** — the default view. An event counts as live if it happened inside
  the last twelve hours. This is the operational feed.
- **Recent · 3d** — one click away. Everything from the last three days.
- **Older than three days is never shown.** The server does not even send it to the
  page. The database keeps it — `vatrocad.sqlite3` is still your archive — but the
  console is not an archive and does not pretend to be one.

Each row carries its age (*2h ago*, *1d ago*), green while it is inside the live
band. The source strip uses the same clock:

- **LIVE** — newest event inside 12 hours.
- **RECENT** — newest event inside 3 days.
- **STALE** — fetches fine, but its newest event is older than that. A site that
  answers `200 OK` with a five-month-old log is stale, and the badge says so rather
  than letting a green HTTP status pass for freshness.
- **QUIET** — a county police desk with nothing incident-shaped stored. Not broken,
  not stale; a quiet county.

Events without a recorded time are treated as 00:00 on their date. That errs
toward dropping out of the live band early, never toward faking freshness. Both
windows are set in one place at the top of `vatrocad.py`: `LIVE_WINDOW_HOURS` and
`MAX_AGE_DAYS`.

Be realistic about what "12 hours" can contain. The brigade logs and police desks
post the morning after; a quiet night can leave the live band nearly empty at
07:00 and fill it by 10:00. That is the sources' rhythm, not a fault in the app.

## Hitna — the whole-country answer

I probed **all 21 county emergency-medicine institutes** (the list comes from
HZHM's own directory). Every one of them: annual reports, board decisions, hiring
notices. The two with a feed item inside the window — Lika-Senj and Karlovac —
were posting job competitions. **No county EMS institute in Croatia publishes
interventions, live or otherwise.** That is a settled negative, not a gap in the
search.

What *does* exist, and is now the backbone of this app, is the **police**. All 20
county police administrations publish per-incident reports daily on one shared
gov.hr CMS: every traffic accident with injuries, every fire they attended — with
the time (*"oko 3:20 sati"*), the place, who was hurt, which ambulance service took
them (*"vozilom Zavoda za hitnu medicinu Međimurske"*), and on fires, which brigade
put it out and when. It is the only nationwide, daily, per-incident emergency feed
Croatia has in the open, and it reads from the EMS side of the same events the
fire sources describe.

On top of that, MUP publishes a national **casualty summary** every few days —
accidents with casualties, serious ones, killed, injured — which the app stores as
a summary row and surfaces in the KPI strip.

Police items are filtered to incidents only: arrests, thefts, drugs and prevention
campaigns are dropped before fetching. Roundup headlines (*"Prometne nesreće
tijekom proteklog vikenda"*) are kept as summaries, not counted as single calls.
The `Nesreće` category and filter hold these; they never masquerade as brigade
calls.

The one thing to hold in mind: a police report is written after the fact, usually
the next morning. The event date inside the text is used when it can be parsed,
so a Monday report of a Saturday-night crash lands on Saturday.

### Why two Međimurje sources are better than one

They report at different resolutions, and together they show something neither
holds alone. On 26 March 2026 the county association logged *"54 intervencije u 31
sat"* after a windstorm; DVD Vratišinec logged four individual fallen-tree calls
across the same 26–27 March window. One village's share of a county-wide night,
visible at both scales. Run this long enough and that pattern repeats.

The county association has `wp-json` disabled, so it is read through the classic
WordPress feed instead — `?cat=3&feed=rss2`, category 3 being *Intervencije*. Worth
remembering when a site looks closed: the old feed endpoints often still answer.

### What Međimurje does not have

Only Vratišinec publishes per-call. I probed the obvious domains for Prelog, Mursko
Središće, Nedelišće, Kotoriba, Podturen, Belica, Goričan, Štrigova, Selnica,
Orehovica, Mala Subotica, Pretetinec, Domašinec, Dekanovec, Strahoninec, Macinec and
Hodošan. Most have no website at all; the rest publish news, not interventions. The
county's brigades overwhelmingly use Facebook, which is neither open nor stable to
parse — deliberately not scraped here.

### Zagreb: the data exists, behind a login

This is the most useful thing to know about Zagreb, and it is not "there is no data".

JVP Zagreb publishes a page called *DVD intervencije i izvješća* with entries for
2024, 2025 and **2026**. Those links go to a live FileMaker database:

```
https://fm.vatrogasci-zagreb.hr/fmi/webd/JVP_Zagreb_2026
```

The server is real and answering — its Data API engine responds unauthenticated:

```
GET /fmi/data/vLatest/productInfo   →  200  {"name":"FileMaker Data API Engine", …}
GET /fmi/data/vLatest/databases     →  401  {"code":"9","message":"Insufficient privileges"}
```

So Zagreb's DVD intervention data is **current, structured, in a real database, and
sitting behind a credential** — with a documented REST API (the FileMaker Data API)
that would serve it directly to this app given an account. If you have standing with
JVP Zagreb or VZ Grada Zagreba, asking for read credentials is a far better route
than anything scrapable. That would be worth more than every source in this table
combined.

I did not attempt to get past that 401, and neither should you without permission.

**What Zagreb publishes openly.** I mined VZ Grada Zagreba's member list for brigade
websites and probed all eleven: Horvati, Hrelić, Sveta Klara, Blato, Gračani,
Trešnjevka, Resnik, Šestine, Sesvete, Botinec and Trnje. Exactly one — **DVD
Horvati** — has an open Intervencije category with current posts (latest 29 March
2026, the windstorm). DVD Sveta Klara has one too but it died in 2021. The rest
publish community news. Horvati is in the table above; it is thin, but it is Zagreb's
only open window.

**Varaždin** has nothing: JVP Varaždin's RSS is stale since February, VZ Varaždinske
and DVD Varaždin publish notices. JVP Čakovec gives annual totals only — 329 calls
in 2025.

### What can actually fill a 12-hour window

Only two kinds of source in Croatia publish fast enough to populate a 12h band:

1. **A brigade that logs every call as it closes.** Exactly one does it —
   **JVP Šibenik**, often within the hour. A call logged at 07:27 was in this
   console by 07:50 the same morning.
2. **A newsroom watching the county.** **eMeđimurje's** *crna kronika* feed runs
   minutes fresh and carries brigade and ambulance call-outs per incident, with
   the settlement in caps at the head of the headline. It is secondary — it
   reports what the services did rather than being their own log — but for
   Međimurje it is by a wide margin the fastest thing that exists. Pure crime
   (burglary, theft, drugs) is filtered out; this is an emergency console, not a
   police blotter.

Everything else in the table publishes on a slower clock: county bulletins twice
daily, police the following morning, the national digest once a night. That is the
sources' rhythm and no amount of polling changes it.

### Searched for more 12-hour sources, found none

Beyond the table, these were probed and rejected on freshness or content:

- **~20 city JVP sites** (Zadar, Pula, Rijeka, Osijek, Karlovac, Sisak, Bjelovar,
  Koprivnica, Krapina, Virovitica, Požega, Velika Gorica and others). Only
  **JVP Požega** had a real per-call log — dead since 2019. JVP Zadar's ŽVOC page
  is a 2012 notice explaining that it briefs *the media* twice daily, which is
  precisely why the county's incident record lives in newsrooms, not on its site.
- **21 county fire associations on `*.spis.hvz.hr`** — HVZ's shared CMS. Not one
  subdomain resolved from here.
- **Regional newsrooms**: `mnovine.hr` (403s on every feed path), `medjimurje.info`
  and `tris.com.hr` (five-item general feed, no incident category).
- **antenazadar.hr** — fire coverage is editorial, not a systematic bulletin.

### Dating sibenik.in

Worth writing down, because it looked impossible at first. sibenik.in articles have
no JSON-LD, no `og:published_time`, no `<time>` element. Its sitemap *does* carry
`<lastmod>` — but that is a rebuild stamp: all 37 crna-kronika articles for
September shared a timestamp within ten minutes of each other. Trusting it would
have dumped a month of archive straight into the live window.

The only real clock is the byline in the article body — `07.09.2026 @ 19:49`. The
parser reads that, and where the bulletin text names the call time (*"dojava
zaprimljena u 14:20h"*) it prefers that instead. **An article whose date cannot be
read is dropped, never guessed at.** A console that invents timestamps is worse
than one with fewer rows.

It earns its place: alongside the ŽVOC bulletins it re-reports, it carries
incidents the bulletins skip — a fatal crash near Trogir, a car fire on a Vodice
car park, a prosecutor's statement on the Pirovac collision.

### Dead ends worth not repeating

- **JVP Vodice** — a per-call log of ~915 entries that stops in October 2021.
- **JVP Drniš** — per-call, stops April 2018.
- **Postman public network, SwaggerHub registry, MCP connector directory** — searched
  for Croatian emergency/fire APIs. Nothing exists.
- **data.gov.hr** — six fire datasets, all registries and strategy PDFs. The one
  national intervention record points at `duzs.hr`, a domain that no longer resolves.
- **Facebook** — where most Croatian volunteer brigades actually post. Not scraped
  here: not open, not stable, and not something to build on.

## Why there's no API to call

Croatian fire dispatch runs on **UVI** (Upravljanje vatrogasnim intervencijama) with
**VATROnet** as the central registry, both operated by HVZ. Every brigade files into
them — the Vratišinec entries say so outright, citing *"putem UVI sustava uzbunjeno"*
as the alerting path, and one call dispatched by *"VOC JVP Čakovec"*.

Access is governed by NN 80/2021 and is not public. There is no open endpoint. These
scraped pages are the only open surface, which is why coverage is two brigades rather
than two hundred. If you have standing to request UVI access, that is a far better
route than this tool.

## Endpoints

| Path | Returns |
|---|---|
| `/` | the dashboard |
| `/api/incidents` | JSON: incidents, context rows, source health |
| `/api/refresh` | forces a poll now, returns `{"new": n}` |

`/api/incidents` is plain JSON — point Grafana, a notebook, or your own UI at it.

## Options

```bash
python3 vatrocad.py --once            # fetch once, print a summary, exit
python3 vatrocad.py --interval 900    # poll every 15 min
python3 vatrocad.py --port 9000 --host 0.0.0.0
```

`--host 0.0.0.0` exposes it to your whole network. There is no authentication, so
only do that on a network you trust.

## Please don't hammer these servers

Several are small volunteer-brigade sites. The 10-minute default with conditional
GET (`If-None-Match` / `If-Modified-Since`) is already lighter than one browser tab
left open on their homepage — a `304 Not Modified` costs them almost nothing. The
interval floor is 60s; there is no reason to go near it. These pages update a few
times a day at most.

The app polls once at start-up and then once per interval — never twice in a row.
The twenty police listings are the heaviest part of a cycle (about 40 s cold, far
less once the validators are warm); article bodies are fetched only for items
inside the live window, capped at four per county per cycle.

## Adding places

Interventions are geocoded against a static gazetteer in `vatrocad.py` — no
geocoding service, so it works offline and leaks nothing. Add entries as you need
them:

```python
PLACES = {
    "vratišinec": (46.496, 16.535),
    "your place": (lat, lon),
}
```

Matching handles Croatian declension (*Pirovca* → Pirovac, *Šibeniku* → Šibenik) by
comparing stems. An unrecognised location still appears in the log; it just gets no
map marker. About 85% of calls place automatically.

## Known limits

- **Locations are settlement centroids, not incident coordinates.** A street address
  resolves to the village. Co-located calls are fanned out on the map so they stay
  clickable, but that spread is cosmetic — it is not positional accuracy.
- **County bulletins restate ongoing incidents.** A multi-day fire appears once per
  bulletin. Records are de-duplicated on content within a source, but the same fire
  legitimately appears from both the station and the county centre, sometimes with
  times differing by a few minutes.
- **Parsers are regex against hand-written HTML.** If a site is redesigned its parser
  returns zero rows and the source strip turns red; nothing else breaks. Fix the
  pattern in that one function.
- **Headlines are machine-trimmed Croatian.** The full original text is kept in the
  `raw` column and shown in the detail panel.

## Layout

```
vatrocad.py        fetch, parse, store, serve
dashboard.html     the console UI
vatrocad.sqlite3   created on first run — your archive
```
