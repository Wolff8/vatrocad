#!/usr/bin/env python3
"""
VatroCAD — a local live console for Croatian fire-service interventions.

Runs entirely on your machine. No API keys, no third-party packages, no cloud
service: it polls the handful of Croatian brigades that publish at per-call
resolution, parses them, stores everything in SQLite, and serves a dispatch-style
dashboard on localhost.

    python3 vatrocad.py                 # serve on http://127.0.0.1:8713
    python3 vatrocad.py --once          # fetch once, print a summary, exit
    python3 vatrocad.py --interval 600  # poll every 10 minutes (default)

Because it keeps its own database, the archive grows past what the sources
themselves retain — most of them only show a rolling window.

Be a good neighbour: these are small volunteer-brigade servers. The default
10-minute interval with conditional GET is already gentler than a browser tab
someone leaves open. Do not lower it much.
"""

import argparse
import html as htmllib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "vatrocad.sqlite3"
# Two windows. The live feed is the last twelve hours — that is the operational
# band. Up to three days is still worth a look, as "recent". Older than three days
# is history: the database keeps it, the dashboard never shows it.
LIVE_WINDOW_HOURS = 12
MAX_AGE_DAYS = 3
LIVE_WINDOW_DAYS = MAX_AGE_DAYS      # used by the police fetcher to bound article fetches
UA = "VatroCAD/1.0 (local dashboard; contact: local user)"
CEST = timezone(timedelta(hours=2))

# Supabase (optional). When both are set, every poll upserts incidents into the
# hosted Postgres so a published dashboard can read them without this machine.
# SUPABASE_KEY must be the SERVICE-ROLE key (it bypasses RLS to write) — keep it
# in the environment / CI secrets, never in the dashboard, which uses the
# read-only publishable key instead.
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")


def status_of(text: str) -> str:
    """Derive a dispatch status from the incident text — the CAD distinction
    between a call that's still running and one that's closed."""
    t = text.lower()
    if re.search(r"aktiv|u tijeku|gašenje traje|još uvijek gori|potraga se nastavlja", t):
        return "active"
    if re.search(r"lokaliziran|pod kontrolom|pod nadzor|stavljen pod", t):
        return "contained"
    if re.search(r"ugašen|ugasili|završen|završil|sanira|spašen|spasili|pronađen|evakuiran|zbrinut", t):
        return "closed"
    return "unknown"


def to_ts(date: str, time_: str):
    """Combine an event date + HH:MM into an ISO timestamp in CEST, for ordering
    and the live window. No time on record → midnight (ages out sooner, never
    fakes freshness)."""
    if not date:
        return None
    hh, mm = (time_.split(":") + ["00", "00"])[:2] if re.match(r"^\d{1,2}:\d{2}$", time_ or "") else ("00", "00")
    try:
        return datetime(int(date[:4]), int(date[5:7]), int(date[8:10]),
                        int(hh), int(mm), tzinfo=CEST).isoformat()
    except (ValueError, IndexError):
        return None


def push_supabase(rows):
    """Upsert parsed incidents into Supabase via the PostgREST endpoint. Idempotent
    (merge on id), so pushing every cycle's full parse is self-healing. No-op when
    Supabase isn't configured. Never raises — a hosting hiccup must not stop polling."""
    if not (SUPABASE_URL and SUPABASE_KEY and rows):
        return "off" if not (SUPABASE_URL and SUPABASE_KEY) else "0"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = []
    for r in rows:
        blob = f"{r.get('title', '')} {r.get('raw', '')}"
        payload.append({
            "id": r["id"], "source": r["source"], "region": r.get("region"),
            "ref": r.get("ref"), "occurred": r.get("date") or None,
            "occurred_time": r.get("time") or None, "ts": to_ts(r.get("date", ""), r.get("time", "")),
            "category": r.get("category"), "status": status_of(blob),
            "title": r.get("title"), "location": r.get("location"),
            "lat": r.get("lat"), "lon": r.get("lon"), "units": r.get("units"),
            "crew": r.get("crew"), "vehicles": r.get("vehicles"),
            "raw": r.get("raw"), "link": r.get("link"), "last_seen": now,
        })
    body = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/incidents?on_conflict=id", data=body, method="POST",
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                 "Content-Type": "application/json",
                 "Prefer": "resolution=merge-duplicates,return=minimal"})
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return f"pushed {len(payload)} ({resp.status})"
    except urllib.error.HTTPError as e:
        return f"error: HTTP {e.code} {e.read()[:120].decode('utf-8', 'replace')}"
    except Exception as e:                                        # noqa: BLE001
        return f"error: {type(e).__name__}"

# --------------------------------------------------------------------------
# Gazetteer. No geocoding service — a static table keeps the app offline-safe.
# Add your own places here; anything unknown still shows in the log, just
# without a map marker.
# --------------------------------------------------------------------------
PLACES = {
    "šibenik": (43.735, 15.889), "ražine": (43.740, 15.920), "brodarica": (43.700, 15.900),
    "mandalina": (43.720, 15.895), "podi": (43.770, 15.950), "dolac": (43.736, 15.890),
    "perković": (43.700, 16.040), "slivno": (43.720, 16.030), "vodice": (43.760, 15.778),
    "pirovac": (43.820, 15.677), "ružić": (43.790, 16.190), "bićine": (43.850, 15.720),
    "drniš": (43.868, 16.155), "knin": (44.041, 16.197), "unešić": (43.783, 16.076),
    "zablaće": (43.708, 15.865), "grebaštica": (43.640, 15.945), "zaton": (43.795, 15.795),
    "bilice": (43.760, 15.930), "krapanj": (43.687, 15.906), "primošten": (43.586, 15.923),
    "skradin": (43.817, 15.922), "tisno": (43.797, 15.632), "murter": (43.822, 15.594),
    # "kljaka" is the genitive of Kljaci — Croatian palatalisation turns the stem's
    # final c into k, which no prefix rule can bridge. Aliases are the honest fix.
    "kljaci": (43.855, 16.145), "kljaka": (43.855, 16.145),
    "radučić": (44.095, 16.115), "bikarac": (43.760, 15.930),
    "jadrtovac": (43.680, 15.980), "boraja": (43.630, 16.020), "konjevrate": (43.795, 16.010),
    "vrpolje": (43.760, 16.020), "rogoznica": (43.532, 15.968), "tribunj": (43.755, 15.745),
    "jezera": (43.782, 15.640), "zlarin": (43.694, 15.845), "prvić": (43.727, 15.792),
    "potkonje": (44.020, 16.220), "žagrović": (44.080, 16.180), "puljane": (43.930, 15.980),
    "miočić": (43.880, 16.170), "golubić": (44.080, 16.250), "kistanje": (43.978, 15.994),
    "oklaj": (43.923, 16.036), "pirovac ": (43.820, 15.677), "betina": (43.828, 15.606),
    # Međimurje — the county is small and dense, so most calls name a village.
    "vratišinec": (46.496, 16.535), "žiškovec": (46.483, 16.512), "čakovec": (46.389, 16.434),
    "prelog": (46.339, 16.616), "nedelišće": (46.383, 16.390), "mursko središće": (46.512, 16.451),
    "pribislavec": (46.392, 16.475), "mala subotica": (46.383, 16.512), "pretetinec": (46.353, 16.334),
    "železna gora": (46.400, 16.220), "selnica": (46.437, 16.290), "orehovica": (46.372, 16.556),
    "podturen": (46.440, 16.573), "kotoriba": (46.348, 16.809), "goričan": (46.383, 16.735),
    "belica": (46.406, 16.549), "domašinec": (46.428, 16.639), "dekanovec": (46.417, 16.653),
    "štrigova": (46.518, 16.259), "sveti martin na muri": (46.518, 16.354),
    "donji kraljevec": (46.375, 16.633), "donji vidovec": (46.340, 16.798),
    "sveta marija": (46.362, 16.756), "šenkovec": (46.400, 16.421), "strahoninec": (46.375, 16.427),
    "macinec": (46.456, 16.399), "hodošan": (46.393, 16.686), "gornji mihaljevec": (46.478, 16.230),
    "sveti juraj na bregu": (46.372, 16.343), "martinuševec": (46.350, 16.310),
    "štefanec": (46.372, 16.408), "gornji kuršanec": (46.360, 16.470),
    "savska ves": (46.372, 16.450), "trnovec": (46.362, 16.470),
    "cirkovljan": (46.352, 16.630), "novakovec": (46.400, 16.600),
    "miklavec": (46.395, 16.640), "totovec": (46.372, 16.487),
    "pribislavec": (46.392, 16.475), "šandorovec": (46.432, 16.484),
    "varaždin": (46.306, 16.338), "zagreb": (45.813, 15.978), "split": (43.510, 16.440),
    # Zagreb districts, for the city's volunteer brigades
    "horvati": (45.773, 15.905), "sveta klara": (45.752, 15.935), "blato": (45.766, 15.892),
    "trnje": (45.797, 15.985), "sesvete": (45.831, 16.115), "gračani": (45.855, 15.965),
    "šestine": (45.848, 15.955), "botinec": (45.757, 15.925), "trešnjevka": (45.800, 15.940),
    "lučko": (45.763, 15.870), "resnik": (45.795, 16.055), "jakuševec": (45.775, 16.000),
    "prevendari": (45.775, 15.900),
    # Zagreb County / Prigorje (prigorski.hr) — the ring around the city
    "sveta nedelja": (45.803, 15.770), "bistra": (45.888, 15.855), "brdovec": (45.867, 15.720),
    "marija gorica": (45.895, 15.720), "pušća": (45.905, 15.780), "dubravica": (45.905, 15.680),
    "oroslavje": (45.995, 15.920), "donja zdenčina": (45.680, 15.760), "pisarovina": (45.620, 15.850),
    "klinča sela": (45.700, 15.750), "rugvica": (45.775, 16.230), "brckovljani": (45.815, 16.310),
    "gradec": (45.900, 16.470), "preseka": (45.940, 16.420), "farkaševac": (45.855, 16.470),
    "bedenica": (45.920, 16.320), "kravarsko": (45.640, 16.010), "orle": (45.700, 16.170),
    "pokupsko": (45.560, 15.985), "žumberak": (45.720, 15.480), "krašić": (45.650, 15.500),
    # Varaždin county (varazdinski.net.hr)
    "varaždinske toplice": (46.208, 16.417), "lepoglava": (46.208, 16.040), "vinica": (46.343, 16.230),
    "cestica": (46.383, 16.150), "petrijanec": (46.320, 16.230), "sračinec": (46.323, 16.283),
    "gornji kneginec": (46.283, 16.383), "jalžabet": (46.283, 16.450), "vidovec": (46.315, 16.325),
    "sveti ilija": (46.280, 16.400), "maruševec": (46.318, 16.180), "bednja": (46.263, 16.020),
    "klenovnik": (46.283, 16.100), "trnovec bartolovečki": (46.335, 16.435), "beretinec": (46.283, 16.300),
    "županac": (46.280, 16.300), "žabnik": (46.290, 16.360),
    "rijeka": (45.330, 14.450), "zadar": (44.120, 15.230), "osijek": (45.550, 18.690),
    "dubrovnik": (42.650, 18.090), "gospić": (44.546, 15.374), "karlovac": (45.487, 15.548),
    # County seats and larger towns, so police reports from anywhere land somewhere sane.
    "krapina": (46.160, 15.874), "sisak": (45.487, 16.376), "koprivnica": (46.163, 16.827),
    "bjelovar": (45.899, 16.848), "virovitica": (45.832, 17.384), "požega": (45.340, 17.685),
    "slavonski brod": (45.160, 18.016), "vukovar": (45.352, 18.999), "pula": (44.867, 13.850),
    "pazin": (45.240, 13.937), "vinkovci": (45.288, 18.805), "đakovo": (45.309, 18.410),
    "samobor": (45.803, 15.711), "velika gorica": (45.712, 16.076), "zaprešić": (45.856, 15.807),
    "dugo selo": (45.807, 16.237), "jastrebarsko": (45.669, 15.649), "sesvete": (45.831, 16.115),
    "ivanić-grad": (45.708, 16.394), "vrbovec": (45.887, 16.423), "sveti ivan zelina": (45.960, 16.245),
    "čazma": (45.750, 16.610), "petrinja": (45.437, 16.290), "kutina": (45.480, 16.780),
    "ogulin": (45.266, 15.230), "senj": (44.990, 14.900), "otočac": (44.869, 15.238),
    # Karlovac county — for the kaportal newsroom feed
    "duga resa": (45.443, 15.492), "ozalj": (45.612, 15.475), "slunj": (45.117, 15.586),
    "vojnić": (45.318, 15.700), "draganić": (45.560, 15.605), "rakovica": (44.987, 15.640),
    "josipdol": (45.180, 15.300), "plaški": (45.077, 15.365), "barilović": (45.375, 15.545),
    "krnjak": (45.353, 15.628), "generalski stol": (45.283, 15.470), "netretić": (45.520, 15.400),
    # Zadar county (zadarskilist) + Koprivnica-Križevci (podravski) newsroom feeds
    "skročine": (44.245, 15.480), "obrovac": (44.201, 15.687), "biograd na moru": (43.938, 15.444),
    "nin": (44.240, 15.180), "pag": (44.446, 15.058), "novalja": (44.554, 14.888),
    "gračac": (44.297, 15.857), "vrsi": (44.278, 15.150), "privlaka": (44.259, 15.150),
    "jazine": (44.115, 15.220), "bokanjac": (44.145, 15.263), "diklo": (44.140, 15.235),
    "koprivnički ivanec": (46.145, 16.850), "đelekovec": (46.223, 16.807), "peteranec": (46.190, 16.900),
    "hlebine": (46.100, 16.988), "drnje": (46.140, 16.850), "molve": (46.078, 17.070),
    "novigrad podravski": (46.116, 16.930), "gola": (46.055, 17.080), "sokolovac": (46.115, 16.720),
    "bosiljevo": (45.400, 15.290), "cetingrad": (45.152, 15.735), "lasinja": (45.505, 15.750),
    "borlin": (45.470, 15.530), "mrežnica": (45.400, 15.480), "korana": (45.230, 15.560),
    "tušilović": (45.400, 15.600), "turanj": (45.470, 15.530),
    "makarska": (43.297, 17.018), "trogir": (43.516, 16.251), "sinj": (43.703, 16.639),
    "imotski": (43.446, 17.217), "omiš": (43.444, 16.689), "solin": (43.543, 16.493),
    "kaštela": (43.550, 16.370), "biograd": (43.938, 15.444), "benkovac": (44.035, 15.613),
    # Split-Dalmatia (dalmacijadanas) + Sisak-Moslavina (sisak.info) newsroom feeds
    "vrgorac": (43.204, 17.375), "hvar": (43.172, 16.443), "brač": (43.320, 16.630),
    "supetar": (43.383, 16.552), "vis": (43.061, 16.183), "dugopolje": (43.573, 16.606),
    "klis": (43.562, 16.523), "podstrana": (43.478, 16.548), "stobreč": (43.505, 16.530),
    "tučepi": (43.267, 17.055), "baška voda": (43.358, 16.948), "trilj": (43.622, 16.727),
    "vrlika": (43.905, 16.398), "hrvace": (43.735, 16.633), "otok": (43.605, 16.703),
    "petrinja": (45.437, 16.290), "kutina": (45.480, 16.780), "novska": (45.339, 16.978),
    "glina": (45.342, 16.092), "hrvatska kostajnica": (45.223, 16.541), "popovača": (45.570, 16.626),
    "sunja": (45.361, 16.545), "topusko": (45.295, 15.968), "gvozd": (45.312, 15.870),
    "lekenik": (45.560, 16.202), "martinska ves": (45.510, 16.320), "dvor": (45.073, 16.378),
    "metković": (43.054, 17.648), "ploče": (43.056, 17.433), "korčula": (42.960, 17.135),
    "crikvenica": (45.176, 14.692), "opatija": (45.336, 14.305), "krk": (45.027, 14.575),
    "delnice": (45.399, 14.802), "poreč": (45.227, 13.594), "rovinj": (45.081, 13.640),
    "umag": (45.435, 13.523), "labin": (45.095, 14.120), "ivanec": (46.224, 16.124),
    "ludbreg": (46.252, 16.618), "novi marof": (46.158, 16.336), "križevci": (46.022, 16.542),
    "đurđevac": (46.038, 17.070), "daruvar": (45.591, 17.225), "našice": (45.492, 18.095),
    "beli manastir": (45.771, 18.605), "županja": (45.072, 18.698), "nova gradiška": (45.255, 17.383),
    "slatina": (45.703, 17.703), "pakrac": (45.436, 17.191), "šandorovec": (46.432, 16.484),
    "vrbovsko": (45.369, 15.078), "dubrava": (45.830, 16.060), "žbandaj": (45.245, 13.667),
    "gradište": (45.320, 17.800), "đulovac": (45.640, 17.360),
    # Massifs / wilderness areas — for HGSS mountain-rescue actions
    "dinara": (43.900, 16.380), "paklenica": (44.300, 15.470), "velebit": (44.530, 15.300),
    "biokovo": (43.320, 17.050), "učka": (45.290, 14.200), "risnjak": (45.420, 14.750),
    "medvednica": (45.900, 15.970), "sljeme": (45.900, 15.970), "mosor": (43.470, 16.620),
    "kozjak": (43.570, 16.450), "vidova gora": (43.280, 16.630), "klek": (45.280, 15.100),
    "samoborsko gorje": (45.780, 15.600), "žumberačko gorje": (45.720, 15.480),
    "bukovac": (43.300, 17.020), "kupa": (45.487, 15.548), "plitvice": (44.880, 15.616),
    "papuk": (45.520, 17.680), "psunj": (45.360, 17.420), "velika kapela": (45.230, 15.000),
}

# County police administrations (policijske uprave). Every one runs the same gov.hr
# CMS, so a single parser reads all twenty. The seat is the geocode fallback.
POLICE_PUS = [
    ("zagrebacka",             "zag", "PU zagrebačka",             "zagreb"),
    ("krapinsko-zagorska",     "kzz", "PU krapinsko-zagorska",     "krapina"),
    ("sisacko-moslavacka",     "smz", "PU sisačko-moslavačka",     "sisak"),
    ("karlovacka",             "kaz", "PU karlovačka",             "karlovac"),
    ("varazdinska",            "vaz", "PU varaždinska",            "varaždin"),
    ("koprivnicko-krizevacka", "kkz", "PU koprivničko-križevačka", "koprivnica"),
    ("bjelovarsko-bilogorska", "bbz", "PU bjelovarsko-bilogorska", "bjelovar"),
    ("primorsko-goranska",     "pgz", "PU primorsko-goranska",     "rijeka"),
    ("licko-senjska",          "lsz", "PU ličko-senjska",          "gospić"),
    ("viroviticko-podravska",  "vpz", "PU virovitičko-podravska",  "virovitica"),
    ("pozesko-slavonska",      "psz", "PU požeško-slavonska",      "požega"),
    ("brodsko-posavska",       "bpz", "PU brodsko-posavska",       "slavonski brod"),
    ("zadarska",               "zdz", "PU zadarska",               "zadar"),
    ("osjecko-baranjska",      "obz", "PU osječko-baranjska",      "osijek"),
    ("sibensko-kninska",       "sib", "PU šibensko-kninska",       "šibenik"),
    ("vukovarsko-srijemska",   "vsz", "PU vukovarsko-srijemska",   "vukovar"),
    ("splitsko-dalmatinska",   "sdz", "PU splitsko-dalmatinska",   "split"),
    ("istarska",               "isz", "PU istarska",               "pula"),
    ("dubrovacko-neretvanska", "dnz", "PU dubrovačko-neretvanska", "dubrovnik"),
    ("medjimurska",            "med", "PU međimurska",             "čakovec"),
]
# Headlines that are police PR rather than an incident. Dropped before fetching.
POLICE_SKIP = ("preventiv", "educira", "akcija", "nadzor", "obavijest", "natječaj", "dan otvorenih",
               "tjedna analiza", "sažetak", "pregled događaja", "uhićen", "provala", "otuđ", "krađ",
               "prijevar", "droga", "kazneno djelo", "remeti", "glazb", "alkohol")
# Crime that is not an emergency call-out. Used to filter media crime sections.
CRIME_SKIP = ("provala", "krađ", "otuđ", "droga", "uhićen", "prijevar", "nasilj", "prijetnj",
              "tučnjav", "remeti", "prekršaj", "kazneno djelo", "razbojni", "pretres",
              # Administrative/enforcement stories that share vocabulary with real
              # interventions but aren't one: a roadworthiness order, a court fine,
              # a licence suspension — police-blotter admin, not a dispatched call.
              "izvanredni tehnički", "oduzeo vozačku", "trajno oduzet", "zabranom vožnje",
              "zabranom upravljanja", "novčano kažnjen", "novčanom kaznom",
              "neisprav", "isključili iz prometa", "isključio iz prometa",
              "isključen iz prometa", "isključili iz promet")

MONTHS_HR = {"siječnja": 1, "veljače": 2, "ožujka": 3, "travnja": 4, "svibnja": 5, "lipnja": 6,
             "srpnja": 7, "kolovoza": 8, "rujna": 9, "listopada": 10, "studenoga": 11,
             "studenog": 11, "prosinca": 12}

CATEGORY_RULES = [
    # Police-reported casualty events. Traffic accidents with injuries are the
    # single largest driver of EMS call-outs, so they belong in a fire+hitna view,
    # but they get their own category so they never masquerade as brigade calls.
    ("accident", ("prometna nesreća", "prometne nesreće", "prometnoj nesreći", "poginu",
                  "smrtno stradal", "teško ozlijeđ", "tesko ozlijed", "lakše ozlijeđ",
                  "nastradal", "utopi", "eksplozij", "pad s visine", "ozlijeđen")),
    ("ems",   ("asistencij", "hmp", "hitne medicinske", "bolesne osobe", "sanitetsk")),
    ("fire",  ("požar", "pozar", "vatrodojav", "užaren", "uzaren", "dim ", "gorenj", "zapalj")),
    ("tech",  ("tehnič", "tehnic", "ispumpav", "crpljen", "saniranj", "krovišt", "prometn nesrec",
               "otvaranje vrata", "spašavanj", "spasavanj")),
]


ROUNDUP = ("vikend", "evidentiran", "tijekom protekl", "prometne nesreće i prekršaji",
           "tjedni pregled", "u proteklih", "u protekla")


def _cat(low: str):
    for cat, keys in CATEGORY_RULES:
        if any(k in low for k in keys):
            return cat
    return None


def categorise(text: str, title: str = "") -> str:
    """Headlines are cleaner than bodies. A body that says 'nobody was hurt' still
    contains the word for 'hurt', so the title decides whenever it can."""
    tl = title.lower()
    if tl and any(k in tl for k in ROUNDUP):
        return "summary"                         # a weekend tally, not one call
    return _cat(tl) or _cat(text.lower()) or "other"


UNIT_RE = re.compile(r"\b(?:JVP|DVD|IVP|VZ\w*|HGSS)\s+[A-ZŠĐČĆŽ][\wšđčćž]*(?:\s*[–-]\s*\w+)?", re.U)
# "na području Perkovića", "u gradskom predjelu Ražine", "u Ulici X" — the phrases
# that actually name where the call was, as opposed to which unit answered it.
LOC_PHRASE = re.compile(
    r"(?:na\s+području|u\s+gradskom\s+predjelu|u\s+naselju|kod|između|u)\s+"
    r"([A-ZŠĐČĆŽ][\wšđčćž]+)", re.U)


def _same_place(word: str, name: str) -> bool:
    """Croatian declension mangles the tail of a place name and can drop the
    fugitive 'a' (Pirovac → Pirovca, Vodice → Vodica, Šibenik → Šibeniku), so
    compare on a shared stem rather than trying to undo every case ending."""
    w, n = word.lower(), name.lower()
    # Short names get no stem latitude at all: 'podi' would otherwise claim
    # Podravina, Podsused and Podgora. Exact word, or nothing.
    if len(n) <= 5:
        # Allow the declined forms (Knin → Kninu, Krk → Krka) but nothing shorter
        # than the name itself — 'pod' is a preposition, not a suburb.
        return w == n or (w.startswith(n) and len(w) <= len(n) + 2)
    k = min(len(w), len(n), max(5, len(n) - 3))
    return len(w) >= 4 and w[:k] == n[:k]


def _match(word: str):
    """Most specific match wins: 'Vrbovskog' shares a stem with both Vrbovec and
    Vrbovsko, and the longer common prefix is the right one."""
    w = word.lower()
    best = None
    for name, coords in PLACES.items():
        if _same_place(word, name):
            common = 0
            for a, b in zip(w, name):
                if a != b:
                    break
                common += 1
            if best is None or common > best[0] or (common == best[0] and len(name) > len(best[1])):
                best = (common, name, coords)
    return (best[1], best[2]) if best else None


def geocode(text: str):
    """Locate the incident, ignoring place names that are only part of a unit name."""
    scrubbed = UNIT_RE.sub(" ", text)
    # First choice: a place named by an explicit location phrase.
    for m in LOC_PHRASE.finditer(scrubbed):
        hit = _match(m.group(1))
        if hit:
            return hit[1]
    # Fallback: longest gazetteer name appearing as a whole word outside a unit
    # name. Whole-word matters — 'podi' is inside 'područje', which is in almost
    # every police report in the country.
    low = scrubbed.lower()
    best = None
    for name, coords in PLACES.items():
        if re.search(r"(?<![\wšđčćž])" + re.escape(name) + r"(?![\wšđčćž])", low) and \
                (best is None or len(name) > len(best[0])):
            best = (name, coords)
    return best[1] if best else (None, None)


# --------------------------------------------------------------------------
# HTTP with conditional GET, so repeat polls cost the source almost nothing.
# --------------------------------------------------------------------------
_CACHE_VALIDATORS = {}


def fetch(url: str, timeout: int = 30):
    """Return (text, status). status is 'ok', 'unchanged', or 'error: ...'."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "hr,en;q=0.8",
    })
    val = _CACHE_VALIDATORS.get(url, {})
    if val.get("etag"):
        req.add_header("If-None-Match", val["etag"])
    if val.get("modified"):
        req.add_header("If-Modified-Since", val["modified"])
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            _CACHE_VALIDATORS[url] = {
                "etag": resp.headers.get("ETag"),
                "modified": resp.headers.get("Last-Modified"),
            }
            charset = resp.headers.get_content_charset()
            for enc in filter(None, (charset, "utf-8", "cp1250", "iso-8859-2")):
                try:
                    return raw.decode(enc), "ok"
                except (UnicodeDecodeError, LookupError):
                    continue
            return raw.decode("utf-8", "replace"), "ok"
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None, "unchanged"
        return None, f"error: HTTP {e.code}"
    except Exception as e:                                    # noqa: BLE001
        return None, f"error: {type(e).__name__}"


html_unescape = htmllib.unescape


def strip_tags(s: str) -> str:
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", "\n", s)
    return htmllib.unescape(s)


def tidy(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------
# Source parsers. Each returns a list of incident dicts and never raises —
# a source that changes its layout degrades to zero rows, it does not take
# the app down.
# --------------------------------------------------------------------------

def src_vratisinec():
    """DVD Vratišinec, Međimurje — open WordPress REST API, numbered per-call."""
    out = []
    url = ("https://dvd-vratisinec.hr/wp-json/wp/v2/posts"
           "?per_page=50&_fields=id,date,title,excerpt,link")
    text, status = fetch(url)
    if text is None:
        return out, status
    try:
        posts = json.loads(text)
    except json.JSONDecodeError:
        return out, "error: bad JSON"
    for p in posts:
        title = tidy(strip_tags(p["title"]["rendered"]))
        # Intervention posts are titled "Something (N/YYYY)". Anything else is news.
        m = re.search(r"\((\d+(?:\s*[–-]\s*\d+)?/\d{4})\)\s*$", title)
        if not m:
            continue
        ref = m.group(1).replace(" ", "")
        clean = title[:m.start()].strip()
        body = tidy(strip_tags(p["excerpt"]["rendered"]))
        tm = re.search(r"\b(\d{1,2}[:.]\d{2})\s*(?:sati|h\b)", body)
        # A single-village brigade: street addresses in the text won't geocode,
        # so fall back to the village itself rather than dropping the marker.
        lat, lon = geocode(clean + " " + body)
        if lat is None:
            lat, lon = PLACES["vratišinec"]
        out.append({
            "id": f"vratisinec:{p['id']}",
            "source": "DVD Vratišinec", "region": "med", "ref": ref,
            "date": p["date"][:10], "time": tm.group(1).replace(".", ":") if tm else "",
            "category": categorise(clean + " " + body), "title": clean,
            "location": "Vratišinec", "lat": lat, "lon": lon,
            "units": "DVD Vratišinec", "crew": None, "vehicles": None,
            "raw": body[:600], "link": p.get("link", ""),
        })
    return out, status


NEWS_ITEM = re.compile(
    r"<div class='news_item'><a href='([^']+)'><span class='h3'>(.*?)</span></a>(.*?)"
    r"<span class='date'>(\d{2})\.(\d{2})\.(\d{4})\.", re.S)


def _event_date(text: str, published: str) -> str:
    """Police write the event date in prose ('U petak, 4. rujna oko 3:20'). Prefer
    it over the publish date, which can trail the event by a day or two."""
    m = re.search(r"(\d{1,2})\.\s*(" + "|".join(MONTHS_HR) + r")(?:\s*(\d{4}))?", text)
    if not m:
        return published
    year = m.group(3) or published[:4]
    try:
        d = datetime(int(year), MONTHS_HR[m.group(2)], int(m.group(1)))
    except ValueError:
        return published
    # A date in the future relative to publication means the year rolled over.
    if d.strftime("%Y-%m-%d") > published:
        d = d.replace(year=d.year - 1)
    return d.strftime("%Y-%m-%d")


def src_police():
    """All twenty county police administrations — per-incident, daily, nationwide.

    This is the closest thing Croatia has to an open EMS feed: every traffic
    accident with injuries, every fire the police attended, with time, place,
    who was hurt and which ambulance service took them. Only the incident
    headlines are kept; arrests, thefts and prevention campaigns are dropped.
    Article bodies are fetched only for items inside the live window, so a poll
    costs about twenty listing requests plus a handful of articles.
    """
    out, worst = [], "ok"
    cutoff = (datetime.now(CEST) - timedelta(days=LIVE_WINDOW_DAYS + 1)).strftime("%Y-%m-%d")
    for slug, region, label, seat in POLICE_PUS:
        base = f"https://{slug}-policija.gov.hr"
        listing, status = fetch(f"{base}/vijesti/8")
        if listing is None:
            if status != "unchanged":
                worst = status
            continue
        fetched = 0
        for href, title_html, excerpt_html, dd, mm, yy in NEWS_ITEM.findall(listing):
            title = tidy(strip_tags(title_html))
            low = title.lower()
            if any(k in low for k in POLICE_SKIP):
                continue
            if categorise(title, title) not in ("accident", "fire", "summary"):
                continue
            published = f"{yy}-{mm}-{dd}"
            if published < cutoff:
                continue
            excerpt = tidy(strip_tags(excerpt_html))
            body = excerpt
            if fetched < 4:                                    # keep polls cheap
                art, _ = fetch(base + href)
                fetched += 1
                if art:
                    plain = tidy(strip_tags(re.sub(r"(?is)<(nav|header|footer)[^>]*>.*?</\1>", " ", art)))
                    i = plain.find(title)
                    body = plain[i + len(title): i + len(title) + 1400] if i >= 0 else plain[:1400]
            date = _event_date(body, published)
            tm = re.search(r"\boko\s+(\d{1,2})[:.,](\d{2})|\bu\s+(\d{1,2})[:.,](\d{2})\s*(?:sati|h\b)", body)
            t = ""
            if tm:
                h, mnt = (tm.group(1), tm.group(2)) if tm.group(1) else (tm.group(3), tm.group(4))
                t = f"{int(h):02d}:{mnt}"
            units = sorted({tidy(u) for u in UNIT_RE.findall(body)})
            ems = "Zavod za hitnu medicinu" in body or "hitne medicinske" in body.lower()
            if ems:
                units.append("ZHM")
            lat, lon = geocode(title + " " + body)
            if lat is None:
                lat, lon = PLACES[seat]
            out.append({
                "id": f"pu:{slug}:{href.rsplit('/', 1)[-1]}",
                "source": label, "region": region, "ref": f"PU-{href.rsplit('/', 1)[-1]}",
                "date": date, "time": t, "category": categorise(body, title),
                "title": title[:110],
                "location": place_label(title + " " + body) or seat.title(),
                "lat": lat, "lon": lon, "units": ", ".join(units) or label,
                "crew": None, "vehicles": None, "raw": body[:600], "link": base + href,
            })
    return out, worst


def src_mup_national():
    """MUP's national 'Sažetak prometnih nesreća' — casualty totals for the whole
    country over a two- or three-day window, published every few days."""
    out = []
    listing, status = fetch("https://policija.gov.hr/vijesti/8")
    if listing is None:
        return out, status
    for href, title_html, _x, dd, mm, yy in NEWS_ITEM.findall(listing)[:20]:
        if "prometnih nesre" not in strip_tags(title_html).lower():
            continue
        art, _ = fetch("https://policija.gov.hr" + href)
        if not art:
            continue
        plain = tidy(strip_tags(art))
        # Two phrasings: a multi-day window "od 4.9.2026. u 00,00 sati do 6.9.2026."
        # and a single day "dana 3.9.2026. od 00,00 do 24,00".
        per = re.search(r"od\s+(\d{1,2}\.\d{1,2}\.\d{4})\.?\s+u.{0,20}?do\s+(\d{1,2}\.\d{1,2}\.\d{4})", plain)
        if not per:
            one = re.search(r"dana\s+(\d{1,2}\.\d{1,2}\.\d{4})", plain)
            per = (one.group(1), one.group(1)) if one else None
        else:
            per = (per.group(1), per.group(2))
        # "dogodila se 21" and "dogodile su se 103" — the reflexive 'se' floats.
        total = re.search(r"dogodil[ae]\s+s[ue](?:\s+se)?\s+(\d+)\s+prometn", plain)
        if not total:
            continue
        words = {"jedna": 1, "dvije": 2, "tri": 3, "četiri": 4, "pet": 5, "šest": 6,
                 "sedam": 7, "osam": 8, "devet": 9, "deset": 10}
        num = lambda s: int(s) if s.isdigit() else words.get(s)
        km = re.search(r"(\d+|jedna|dvije|tri|četiri|pet|šest|sedam|osam|devet|deset)\s+osob[ae]?\s+(?:su|je)\s+smrtno", plain)
        if km:
            killed = num(km.group(1))
        elif re.search(r"Nije bilo nesreća s poginulim", plain):
            killed = 0
        else:
            killed = None
        inj = re.search(r"Ukupno\s+(?:je|su)\s+ozlijeđen[oae]\s+(\d+)", plain)
        injured = int(inj.group(1)) if inj else None
        ser = re.search(r"(\d+|jedna|dvije|tri|četiri|pet|šest|sedam|osam|devet)\s+pripada(?:ju)?\s+kategoriji\s+teških", plain)
        serious = num(ser.group(1)) if ser else None
        end = per[1] if per else f"{dd}.{mm}.{yy}"
        d, m, y = end.split(".")
        out.append({
            "id": f"mup:{href.rsplit('/', 1)[-1]}",
            "source": "MUP · nacionalno", "region": "nat", "ref": f"MUP-{href.rsplit('/', 1)[-1]}",
            "date": f"{y}-{int(m):02d}-{int(d):02d}", "time": "24:00", "category": "summary",
            "title": f"Prometne nesreće s nastradalima — {total.group(1)} u RH"
                     + (f", {per[0]}" + (f"–{per[1]}" if per[1] != per[0] else "") if per else ""),
            "location": "Republika Hrvatska", "lat": None, "lon": None,
            "units": f"{killed if killed is not None else '?'} killed · {injured if injured is not None else '?'} injured",
            "crew": int(total.group(1)), "vehicles": None,
            "raw": json.dumps({"accidents_with_casualties": int(total.group(1)), "serious": serious,
                               "killed": killed, "injured": injured,
                               "window": list(per) if per else None}, ensure_ascii=False),
            "link": "https://policija.gov.hr" + href,
        })
    return out, status


# Regional-newsroom "crna kronika" feeds. Each is a WordPress category RSS that
# carries brigade/EMS/accident incidents per item, usually within hours — the
# fastest live layer for counties whose brigades don't self-publish. They are
# all read by one parser; a new region is one row here, not a new function.
#   (label, region, url, fallback place-key, id-prefix, caps_lead)
# caps_lead=True: the headline opens with the settlement in capitals
# ("ŠTEFANEC Sudarila se…"), which pins the location; most portals don't.
NEWSROOMS = [
    ("eMeđimurje · kronika", "med", "https://emedjimurje.net.hr/category/crna-kronika/feed/",
     "čakovec", "emed", True),
    ("kaportal · kronika", "kaz", "https://kaportal.net.hr/kategorija/crna-kronika/feed/",
     "karlovac", "kap", False),
    ("Dalmacija danas · kronika", "sdz", "https://dalmacijadanas.hr/rubrika/crna-kronika/feed/",
     "split", "dald", False),
    ("sisak.info · kronika", "smz", "https://sisak.info/kategorija/crna-kronika/feed/",
     "sisak", "sisk", False),
    ("Zadarski list · kronika", "zdz", "https://www.zadarskilist.hr/category/crna-kronika/feed/",
     "zadar", "zdl", False),
    ("Podravski · kronika", "kkz", "https://www.podravski.hr/kategorija/crna-kronika/feed/",
     "koprivnica", "podr", False),
    # (prigorski.hr checked and rejected: it aggregates national crna-kronika —
    #  Split/Perković/Ogulin rows would double-count under a wrong Zagreb region.)
    ("Varaždinski · kronika", "vaz", "https://varazdinski.net.hr/kategorija/crna-kronika/feed/",
     "varaždin", "vzd", False),
    ("Međimurski · kronika", "med", "https://medjimurski.hr/category/crna-kronika/feed/",
     "čakovec", "mgr", False),
    ("Požeški · kronika", "psz", "https://pozeski.hr/category/crna-kronika/feed/",
     "požega", "poz", False),
    ("Bjelovar.live · kronika", "bbz", "https://bjelovar.live/category/crna-kronika/feed/",
     "bjelovar", "bjl", False),
]


def make_newsroom(label, region, url, fallback_key, prefix, caps_lead):
    """Build a fetcher for one newsroom crna-kronika feed. Closes over the config
    so every regional newsroom shares one tested parser."""

    def fetch_source():
        out = []
        text, status = fetch(url)
        if text is None:
            return out, status
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return out, "error: bad XML"
        for it in root.findall(".//item"):
            title = tidy(strip_tags(it.findtext("title") or ""))
            body = tidy(strip_tags(it.findtext("description") or ""))
            blob = title + " " + body
            if any(k in blob.lower() for k in CRIME_SKIP):
                continue
            cat = categorise(body, title)
            if cat not in ("fire", "accident", "tech", "ems"):
                continue
            pub = it.findtext("pubDate") or ""
            try:
                dt = datetime.strptime(pub[5:25].strip(), "%d %b %Y %H:%M:%S")
            except ValueError:
                continue
            date, t = dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
            # An hour named inside the story beats the publication hour.
            tm = re.search(r"\b(?:oko|u)\s+(\d{1,2})[:.](\d{2})\s*(?:sati|h\b)", body)
            if tm:
                t = f"{int(tm.group(1)):02d}:{tm.group(2)}"
            lead = re.match(r"^([A-ZŠĐČĆŽ][A-ZŠĐČĆŽ\s]{2,28}?)\s+[A-ZŠĐČĆŽ][a-zšđčćž]",
                            title) if caps_lead else None
            lat, lon = geocode((lead.group(1) + " " if lead else "") + blob)
            if lat is None:
                lat, lon = PLACES[fallback_key]
            out.append({
                "id": f"{prefix}:{tidy(it.findtext('guid') or it.findtext('link') or title)[-40:]}",
                "source": label, "region": region,
                "ref": f"{prefix.upper()}-{dt:%m%d-%H%M}", "date": date, "time": t,
                "category": cat, "title": title[:110],
                "location": (lead.group(1).title() if lead and _match(lead.group(1)) else None)
                            or place_label(blob) or fallback_key.title(),
                "lat": lat, "lon": lon,
                "units": ", ".join(sorted({tidy(u) for u in UNIT_RE.findall(blob)})) or "—",
                "crew": None, "vehicles": None, "raw": body[:600],
                "link": tidy(it.findtext("link") or ""),
            })
        return out, status

    return fetch_source


def src_dvd_horvati():
    """DVD Horvati, Zagreb — the only Zagreb brigade found publishing interventions.

    Sporadic rather than per-call: notable incidents and deployments, a few posts a
    year. It is here because it is the *only* open Zagreb source. The city's real
    per-call data (JVP Zagreb's 'DVD intervencije i izvješća') sits in a credentialed
    FileMaker database at fm.vatrogasci-zagreb.hr — see the README.
    """
    out = []
    cats, status = fetch("https://dvd-horvati.hr/wp-json/wp/v2/categories"
                         "?per_page=50&_fields=name,count,id")
    if cats is None:
        return out, status
    try:
        cid = next(c["id"] for c in json.loads(cats)
                   if "interven" in c["name"].lower() and c["count"])
    except (StopIteration, json.JSONDecodeError):
        return out, "error: no Intervencije category"
    text, status = fetch(f"https://dvd-horvati.hr/wp-json/wp/v2/posts"
                         f"?categories={cid}&per_page=30&_fields=id,date,title,excerpt,link")
    if text is None:
        return out, status
    try:
        posts = json.loads(text)
    except json.JSONDecodeError:
        return out, "error: bad JSON"
    for p in posts:
        title = tidy(strip_tags(p["title"]["rendered"]))
        body = tidy(strip_tags(p["excerpt"]["rendered"]))
        tm = re.search(r"\bu?\s*(\d{1,2})[:.](\d{2})\s*(?:sati|h\b)", body)
        lat, lon = geocode(title + " " + body)
        if lat is None:
            lat, lon = PLACES["horvati"]
        out.append({
            "id": f"horvati:{p['id']}",
            "source": "DVD Horvati", "region": "zag", "ref": f"ZG-{p['date'][:10]}",
            "date": p["date"][:10],
            "time": f"{int(tm.group(1)):02d}:{tm.group(2)}" if tm else "",
            "category": categorise(title + " " + body), "title": title[:110],
            "location": place_label(title + " " + body) or "Horvati, Zagreb",
            "lat": lat, "lon": lon, "units": "DVD Horvati",
            "crew": None, "vehicles": None,
            "raw": body[:600], "link": p.get("link", ""),
        })
    return out, status


def src_vz_medjimurje():
    """VZ Međimurske županije — county association, 'Intervencije' category.

    The site has wp-json disabled, but classic WordPress feeds are open, and
    category 3 is Intervencije. Event-driven rather than per-call: notable
    incidents plus county-wide summaries after a big night.
    """
    out = []
    text, status = fetch("https://vatrogasci-medjimurja.eu/?cat=3&feed=rss2")
    if text is None:
        return out, status
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return out, "error: bad XML"
    months = {"siječnja": 1, "veljače": 2, "ožujka": 3, "travnja": 4, "svibnja": 5, "lipnja": 6,
              "srpnja": 7, "kolovoza": 8, "rujna": 9, "listopada": 10, "studenoga": 11,
              "studenog": 11, "prosinca": 12}
    for it in root.findall(".//item"):
        title = tidy(strip_tags(it.findtext("title") or ""))
        body = tidy(strip_tags(it.findtext("description") or ""))
        pub = it.findtext("pubDate") or ""
        # Prefer the date written in the text; fall back to the publish date.
        date = ""
        dm = re.search(r"(\d{1,2})\.\s*(" + "|".join(months) + r")\s*(\d{4})", body)
        if dm:
            date = f"{dm.group(3)}-{months[dm.group(2)]:02d}-{int(dm.group(1)):02d}"
        else:
            try:
                date = datetime.strptime(pub[5:16].strip(), "%d %b %Y").strftime("%Y-%m-%d")
            except ValueError:
                date = datetime.now(CEST).strftime("%Y-%m-%d")
        tm = re.search(r"\bu\s+(\d{1,2})[.:](\d{2})\s*(?:sat|h\b)", body)
        t = f"{int(tm.group(1)):02d}:{tm.group(2)}" if tm else ""
        # A county-wide tally ("54 intervencije u 31 sat") is a summary, not one call.
        tally = re.search(r"(\d+)\s+intervencij", title)
        units = sorted({tidy(u) for u in UNIT_RE.findall(title + " " + body)})
        lat, lon = geocode(title + " " + body)
        if lat is None:
            lat, lon = PLACES["čakovec"]
        out.append({
            "id": rowid("VZ Međimurske", date, t, title),
            "source": "VZ Međimurske ž.", "region": "med",
            "ref": f"MŽ-{date[5:7]}{date[8:10]}",
            "date": date, "time": t,
            "category": "summary" if tally else categorise(title + " " + body),
            "title": title[:110], "location": place_label(title + " " + body) or "Međimurje",
            "lat": lat, "lon": lon, "units": ", ".join(units) or "VZ Međimurske županije",
            "crew": int(tally.group(1)) if tally else None, "vehicles": None,
            "raw": body[:600], "link": tidy(it.findtext("link") or ""),
        })
    return out, status


def src_sibenik_in():
    """sibenik.in 'crna kronika' — Šibenik-Knin county newsroom.

    Carries the ŽVOC twelve-hour bulletins, usually with more narrative than the
    association's own page, plus incidents the bulletins skip. Secondary, like
    eMeđimurje.

    Dating this one took some doing: the articles carry no JSON-LD, no
    og:published_time and no <time> element, and the sitemap's <lastmod> is a
    rebuild stamp (every article in a month shares one timestamp). The only
    honest clock is the byline in the body — "07.09.2026 @ 19:49". An article
    whose date cannot be read is dropped rather than guessed at; a console that
    invents timestamps is worse than one with fewer rows.
    """
    out = []
    listing, status = fetch("https://www.sibenik.in/crna-kronika")
    if listing is None:
        return out, status
    seen, arts = set(), []
    for url, title in re.findall(
            r'href="(https://www\.sibenik\.in/crna-kronika/[a-z0-9-]+/)"\s*\n?\s*title="([^"]{10,160})"',
            listing):
        if url in seen:
            continue
        seen.add(url)
        arts.append((url, tidy(html_unescape(title))))
    cutoff = (datetime.now(CEST) - timedelta(days=MAX_AGE_DAYS)).strftime("%Y-%m-%d")
    for url, title in arts[:10]:                       # newest first; keep polls cheap
        if any(k in title.lower() for k in CRIME_SKIP):
            continue
        art, _ = fetch(url)
        if not art:
            continue
        plain = tidy(strip_tags(re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>",
                                       " ", art)))
        stamp = re.search(r"(\d{2})\.(\d{2})\.(\d{4})\s*@\s*(\d{1,2}):(\d{2})", plain)
        if not stamp:
            continue                                   # undatable → not stored
        dd, mm, yy, hh, mi = stamp.groups()
        date = f"{yy}-{mm}-{dd}"
        if date < cutoff:
            continue
        i = plain.find(title)
        body = plain[i + len(title): i + len(title) + 1200] if i >= 0 else plain[:1200]
        blob = title + " " + body
        if any(k in blob.lower() for k in CRIME_SKIP):
            continue
        cat = categorise(body, title)
        if cat not in ("fire", "accident", "tech", "ems", "summary"):
            continue
        # The bulletin text usually names the call time; prefer it over the byline.
        tm = re.search(r"(?:dojav[ae]\s+(?:je\s+)?zaprimljena\s+u|u)\s+(\d{1,2})[:.](\d{2})\s*(?:h|sati)",
                       body)
        t = f"{int(tm.group(1)):02d}:{tm.group(2)}" if tm else f"{int(hh):02d}:{mi}"
        lat, lon = geocode(blob)
        if lat is None:
            lat, lon = PLACES["šibenik"]
        out.append({
            "id": f"sibin:{url.rstrip('/').rsplit('/', 1)[-1][:44]}",
            "source": "sibenik.in · kronika", "region": "sib",
            "ref": f"SI-{dd}{mm}-{hh}{mi}", "date": date, "time": t, "category": cat,
            "title": title[:110], "location": place_label(blob) or "Šibenik-Knin",
            "lat": lat, "lon": lon,
            "units": ", ".join(sorted({tidy(u) for u in UNIT_RE.findall(blob)})) or "—",
            "crew": None, "vehicles": None, "raw": body[:600], "link": url,
        })
    return out, status


def src_jvp_sibenik():
    """JVP Šibenik — per-call log, read from the paginated archive.

    The homepage shows about ten calls and renders the block twice; the archive
    at /index.php/intervencije pages through roughly 3,180 entries, thirty at a
    time. Two pages is a comfortable margin over anything the live window needs.
    """
    out = []
    pages, status = [], "ok"
    for start in (0, 30):
        page, st = fetch(f"https://jvp-sibenik.hr/index.php/intervencije?limit=30&start={start}")
        if page:
            pages.append(page)
        elif st != "unchanged":
            status = st
    if not pages:
        return out, status
    plain = re.sub(r"[ \t]+", " ", strip_tags("\n".join(pages)))
    # Blocks look like:  DD.MM.YYYY.  \n  U HH:MM zaprimljena je dojava o ...
    pattern = re.compile(r"(\d{2}\.\d{2}\.\d{4})\.\s*\n\s*(U\s+\d{1,2}:\d{2}[^\n]{0,900})")
    for i, m in enumerate(pattern.finditer(plain)):
        d, body = m.group(1), tidy(m.group(2))
        day, mon, yr = d.split(".")
        tm = re.search(r"U\s+(\d{1,2}:\d{2})", body)
        crew = re.search(r"(\d+|jedn|dva|tri|četiri|pet|šest|sedam|osam|devet|deset)\s+vatrogas", body)
        veh = re.search(r"(\d+|jedn|dva|tri|četiri|pet|šest)\s+vozil", body)
        words = {"jedn": 1, "dva": 2, "tri": 3, "četiri": 4, "pet": 5,
                 "šest": 6, "sedam": 7, "osam": 8, "devet": 9, "deset": 10}
        def num(mt):
            if not mt:
                return None
            v = mt.group(1)
            return int(v) if v.isdigit() else words.get(v)
        units = sorted({tidy(u) for u in UNIT_RE.findall(body)})
        lat, lon = geocode(body)
        t = hhmm(tm.group(1) if tm else "")
        out.append({
            "id": rowid("JVP Šibenik", f"{yr}-{mon}-{day}", t, body),
            "source": "JVP Šibenik", "region": "sib", "ref": f"ŠI-{day}{mon}-{t.replace(':','')}",
            "date": f"{yr}-{mon}-{day}", "time": t,
            "category": categorise(body), "title": summarise(body),
            "location": place_label(body) or "Šibenik", "lat": lat, "lon": lon,
            "units": ", ".join(tidy(u) for u in units) or "JVP Šibenik",
            "crew": num(crew), "vehicles": num(veh),
            "raw": body[:600], "link": "https://jvp-sibenik.hr/",
        })
    return out, status


def src_zvoc_sibenik():
    """ŽVOC Šibenik-Knin — county bulletins, twice daily, bulleted events."""
    out = []
    text, status = fetch("https://www.vatrogastvo-sibenik-knin.hr/stranice/intervencije/")
    if text is None:
        return out, status
    plain = re.sub(r"[ \t]+", " ", strip_tags(text))
    blocks = re.split(r"Priopćenje o vatrogasnim događajima\s*(\d{2}\.\d{2}\.\d{4}),\s*(\d{2}:\d{2})", plain)
    # blocks = [pre, date, time, body, date, time, body, ...]
    for i in range(1, len(blocks) - 2, 3):
        d, t, body = blocks[i], blocks[i + 1], blocks[i + 2]
        day, mon, yr = d.split(".")
        for j, item in enumerate(re.findall(r"^\s*-\s*(.+)$", body, re.M)):
            item = tidy(item)
            if len(item) < 20:
                continue
            tm = re.search(r"(?:zaprimljena|dojava)[^\d]{0,20}(\d{1,2}:\d{2})", item)
            crew = re.search(r"(\d+)\s+vatrogas", item)
            veh = re.search(r"(\d+)\s+vozil", item)
            units = sorted({tidy(u) for u in UNIT_RE.findall(item)})
            lat, lon = geocode(item)
            it = hhmm(tm.group(1)) if tm else t
            out.append({
                "id": rowid("ŽVOC Šibenik-Knin", f"{yr}-{mon}-{day}", it, item),
                "source": "ŽVOC Šibenik-Knin", "region": "sib",
                "ref": f"ŽV-{day}{mon}-{j+1}",
                "date": f"{yr}-{mon}-{day}", "time": it,
                "category": categorise(item), "title": summarise(item),
                "location": place_label(item) or "Šibenik-Knin", "lat": lat, "lon": lon,
                "units": ", ".join(units), "crew": int(crew.group(1)) if crew else None,
                "vehicles": int(veh.group(1)) if veh else None,
                "raw": item[:600],
                "link": "https://www.vatrogastvo-sibenik-knin.hr/stranice/intervencije/",
            })
    return out, status


def src_dvoc_national():
    """HVZ DVOC 193 — national nightly digest. Stored as a daily summary row."""
    out = []
    listing, status = fetch("https://hvz.gov.hr/vijesti/8")
    if listing is None:
        return out, status
    links = sorted({m for m in re.findall(r"/vijesti/dvoc-[a-z0-9-]+/(\d+)", listing)},
                   key=int, reverse=True)[:3]
    for art_id in links:
        text, st = fetch(f"https://hvz.gov.hr/vijesti/dvoc-x/{art_id}")
        if text is None:
            # slug matters to the CMS; fall back to finding the full path
            m = re.search(rf"/vijesti/(dvoc-[a-z0-9-]+)/{art_id}", listing)
            if not m:
                continue
            text, st = fetch(f"https://hvz.gov.hr/vijesti/{m.group(1)}/{art_id}")
            if text is None:
                continue
        plain = tidy(strip_tags(text))
        nums = re.search(
            r"zabilježen[ao]?\s+(?:je\s+)?([\d.]+)\s+vatrogasn\w*\s+intervencij\w*.{0,120}?"
            r"([\d.]+)\s+vatrogasnih\s+organizacij\w*.{0,60}?([\d.]+)\s+vatrogasca.{0,60}?"
            r"([\d.]+)\s+vatrogasnih\s+vozila", plain)
        per = re.search(r"za\s+dane?\s+(.{5,40}?)\s+godine", plain)
        if not nums:
            continue
        ha = re.search(r"opožarena\s+površina[^\d]{0,40}([\d.,]+)\s*ha", plain)
        air = re.search(r"([\d.]+)\s+protupožarnih\s+zrakoplova", plain)
        i2 = lambda s: int(s.replace(".", ""))
        # The report is for a 07:00–07:00 window ending on the second day of
        # "za dane 06. / 07. rujna 2026." — date the row to that day, not to when
        # we happened to fetch it, or the 3-day rule lies.
        rep_date = datetime.now(CEST).strftime("%Y-%m-%d")
        pm = re.search(r"(\d{1,2})\.\s*/\s*(\d{1,2})\.\s*(" + "|".join(MONTHS_HR) + r")\s*(\d{4})",
                       per.group(1) if per else "")
        if pm:
            try:
                rep_date = datetime(int(pm.group(4)), MONTHS_HR[pm.group(3)],
                                    int(pm.group(2))).strftime("%Y-%m-%d")
            except ValueError:
                pass
        out.append({
            "id": f"dvoc:{art_id}",
            "source": "HVZ · DVOC 193", "region": "nat", "ref": f"DVOC-{art_id}",
            "date": rep_date, "time": "07:00",
            "category": "summary",
            "title": f"National nightly digest — {i2(nums.group(1))} interventions",
            "location": per.group(1) if per else "Republika Hrvatska",
            "lat": None, "lon": None,
            "units": f"{i2(nums.group(2))} organisations",
            "crew": i2(nums.group(3)), "vehicles": i2(nums.group(4)),
            "raw": json.dumps({
                "interventions": i2(nums.group(1)), "organisations": i2(nums.group(2)),
                "firefighters": i2(nums.group(3)), "vehicles": i2(nums.group(4)),
                "aircraft": i2(air.group(1)) if air else None,
                "hectares": float(ha.group(1).replace(".", "").replace(",", ".")) if ha else None,
            }, ensure_ascii=False),
            "link": f"https://hvz.gov.hr/vijesti/{art_id}",
        })
    return out, status


def src_meteoalarm():
    """Meteoalarm CAP feed — weather warnings as fire-risk context."""
    out = []
    text, status = fetch("https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-croatia")
    if text is None:
        return out, status
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return out, "error: bad XML"
    ns = {"a": "http://www.w3.org/2005/Atom",
          "cap": "urn:oasis:names:tc:emergency:cap:1.2"}
    now = datetime.now(timezone.utc)
    for i, e in enumerate(root.findall("a:entry", ns)):
        g = lambda p: (e.findtext(p, namespaces=ns) or "").strip()
        expires = g("cap:expires")
        try:
            if expires and datetime.fromisoformat(expires) < now:
                continue                                  # drop lapsed warnings
        except ValueError:
            pass
        area = g("cap:areaDesc")
        lat, lon = geocode(area.replace(" region", ""))
        out.append({
            "id": f"meteo:{area}:{g('cap:event')}:{g('cap:onset')}",
            "source": "Meteoalarm", "region": "nat", "ref": f"WX-{i+1}",
            "date": (g("cap:onset") or "")[:10], "time": (g("cap:onset") or "")[11:16],
            "category": "weather", "title": g("cap:event"),
            "location": area, "lat": lat, "lon": lon,
            "units": g("cap:severity"), "crew": None, "vehicles": None,
            "raw": f"{g('cap:severity')} · {g('cap:certainty')} · expires {expires}",
            "link": "https://meteoalarm.org",
        })
    return out, status


# HGSS station name (from the RSS <category>) → its home town, used as the
# geocode fallback when the incident title names only a massif we don't have.
HGSS_STATIONS = {
    "karlovac": "karlovac", "makarska": "makarska", "samobor": "samobor", "šibenik": "šibenik",
    "split": "split", "zagreb": "zagreb", "rijeka": "rijeka", "zadar": "zadar", "gospić": "gospić",
    "knin": "knin", "dubrovnik": "dubrovnik", "pula": "pula", "orahovica": "orehovica",
    "ogulin": "ogulin", "delnice": "delnice", "varaždin": "varaždin", "čakovec": "čakovec",
    "osijek": "osijek", "koprivnica": "koprivnica", "bjelovar": "bjelovar", "požega": "požega",
}
# HGSS posts that aren't a rescue call: training, drills, ceremonies, admin.
HGSS_SKIP = ("trenažir", "trening", "vježb", "obuk", "predavanj", "sjednic", "obljetnic",
             "izbor", "skupštin", "tečaj", "edukacij", "natjecanj", "prezentacij",
             "donacij", "obiljež", "susret", "godišnj", "izložb", "radionic")


def src_hgss():
    """HGSS — Hrvatska gorska služba spašavanja (Croatian Mountain Rescue).

    A genuinely new intervention *type*: wilderness search-and-rescue nationwide —
    helicopter medevacs off Dinara, river rescues on the Kupa, missing-person and
    downed-aircraft searches. National, so region is 'nat' and each item geocodes
    to the massif in its title (falling back to the responding station's town,
    read from the RSS <category>). Training and ceremony posts are filtered out.
    """
    out = []
    text, status = fetch("https://www.hgss.hr/feed/")
    if text is None:
        return out, status
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return out, "error: bad XML"
    for it in root.findall(".//item"):
        title = tidy(strip_tags(it.findtext("title") or ""))
        body = tidy(strip_tags(it.findtext("description") or ""))
        if not title or any(k in (title + " " + body).lower() for k in HGSS_SKIP):
            continue
        # Keep only genuine rescue/search actions.
        if not re.search(r"spaša|spasi|potra[gž]|nestal|unesreć|ozlijeđ|evakuacij|"
                         r"pronaš|akcij|nesreć|utopi", (title + " " + body).lower()):
            continue
        pub = it.findtext("pubDate") or ""
        try:
            dt = datetime.strptime(pub[5:25].strip(), "%d %b %Y %H:%M:%S")
        except ValueError:
            continue
        station = ""
        for c in it.findall("category"):
            m = re.search(r"stanic[ae]\s+(\w+)", (c.text or ""), re.I)
            if m:
                station = m.group(1).lower()
                break
        lat, lon = geocode(title + " " + body)
        if lat is None and station in HGSS_STATIONS:
            lat, lon = PLACES.get(HGSS_STATIONS[station], (None, None))
        loc = place_label(title + " " + body) or (station.title() if station else "Republika Hrvatska")
        out.append({
            "id": f"hgss:{tidy(it.findtext('guid') or it.findtext('link') or title)[-40:]}",
            "source": "HGSS · spašavanje", "region": "nat",
            "ref": f"HGSS-{dt:%m%d}", "date": dt.strftime("%Y-%m-%d"), "time": dt.strftime("%H:%M"),
            "category": "rescue", "title": title[:110], "location": loc,
            "lat": lat, "lon": lon,
            "units": f"HGSS {('Stanica ' + station.title()) if station else ''}".strip(),
            "crew": None, "vehicles": None, "raw": body[:600],
            "link": tidy(it.findtext("link") or ""),
        })
    return out, status


def place_label(text: str):
    scrubbed = UNIT_RE.sub(" ", text)
    for m in LOC_PHRASE.finditer(scrubbed):
        hit = _match(m.group(1))
        if hit:
            return hit[0].title()
    low = scrubbed.lower()
    hits = [n for n in PLACES
            if re.search(r"(?<![\wšđčćž])" + re.escape(n) + r"(?![\wšđčćž])", low)]
    return max(hits, key=len).title() if hits else None


# "dojava o požaru" is dative; the headline wants the nominative.
DATIVE = {"požaru": "Požar", "tehničkoj": "Tehnička", "potrebi": "", "uklanjanju": "Uklanjanje",
          "ispumpavanju": "Ispumpavanje", "asistenciji": "Asistencija", "izvidu": "Izvid",
          "vatrodojavi": "Vatrodojava", "spašavanju": "Spašavanje", "zapreci": "Zapreka"}


def summarise(body: str, limit: int = 88) -> str:
    """First clause of the narrative, normalised into a headline."""
    m = re.search(r"dojav[ua]\s+o\s+(.{6,120}?)(?:\.|,|\s+[Nn]a intervenciju)", body, re.I)
    s = tidy(m.group(1)) if m else tidy(re.split(r"(?<=[.!?])\s", body)[0])
    first = s.split(" ", 1)
    if first[0].lower() in DATIVE:
        head = DATIVE[first[0].lower()]
        s = (head + " " + first[1]).strip() if len(first) > 1 else head or s
    s = s[:1].upper() + s[1:] if s else body[:limit]
    return s if len(s) <= limit else s[: limit - 1].rstrip(" ,;") + "…"


def hhmm(t: str) -> str:
    """Zero-pad H:MM to HH:MM so the log column stays aligned."""
    m = re.match(r"^(\d{1,2}):(\d{2})$", t or "")
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else (t or "")


def rowid(source: str, date: str, time_: str, body: str) -> str:
    """Stable key. Several of these sites render the same log block twice on one
    page, and bulletins restate an ongoing incident — key on content, not position."""
    import hashlib
    seed = f"{source}|{date}|{time_}|{tidy(body)[:70].lower()}"
    return f"{source.split()[0].lower()}:{hashlib.md5(seed.encode()).hexdigest()[:14]}"


SOURCES = [
    ("DVD Vratišinec",    src_vratisinec,    "WordPress REST API"),
    ("VZ Međimurske ž.",  src_vz_medjimurje, "WP classic RSS, cat=3"),
    *[(label, make_newsroom(label, region, url, fk, pfx, caps), "media · WP category RSS")
      for (label, region, url, fk, pfx, caps) in NEWSROOMS],
    ("DVD Horvati",       src_dvd_horvati,   "WordPress REST API (Zagreb)"),
    ("Policija · 20 PU",  src_police,        "gov.hr CMS, 20 county listings"),
    ("MUP · nacionalno",  src_mup_national,  "HTML, casualty summary"),
    ("JVP Šibenik",       src_jvp_sibenik,   "HTML archive, 2 pages"),
    ("sibenik.in · kronika", src_sibenik_in,  "media · HTML + byline clock"),
    ("ŽVOC Šibenik-Knin", src_zvoc_sibenik,  "HTML county bulletin"),
    ("HVZ · DVOC 193",    src_dvoc_national, "HTML national digest"),
    ("HGSS · spašavanje", src_hgss,          "RSS · mountain rescue"),
    ("Meteoalarm",        src_meteoalarm,    "CAP 1.2 Atom feed"),
]


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents(
  id TEXT PRIMARY KEY, source TEXT, region TEXT, ref TEXT, date TEXT, time TEXT,
  category TEXT, title TEXT, location TEXT, lat REAL, lon REAL, units TEXT,
  crew INTEGER, vehicles INTEGER, raw TEXT, link TEXT,
  first_seen TEXT, last_seen TEXT);
CREATE INDEX IF NOT EXISTS ix_inc_date ON incidents(date DESC, time DESC);
CREATE TABLE IF NOT EXISTS sources(
  name TEXT PRIMARY KEY, method TEXT, status TEXT, count INTEGER, checked TEXT);
"""


def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def store(conn, rows):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new = 0
    with conn:
        for r in rows:
            cur = conn.execute("SELECT 1 FROM incidents WHERE id=?", (r["id"],))
            if cur.fetchone():
                conn.execute("UPDATE incidents SET last_seen=? WHERE id=?", (now, r["id"]))
                continue
            conn.execute(
                """INSERT INTO incidents(id,source,region,ref,date,time,category,title,
                   location,lat,lon,units,crew,vehicles,raw,link,first_seen,last_seen)
                   VALUES(:id,:source,:region,:ref,:date,:time,:category,:title,:location,
                   :lat,:lon,:units,:crew,:vehicles,:raw,:link,:fs,:fs)""",
                {**r, "fs": now})
            new += 1
    return new


def poll_once(conn, verbose=True):
    total_new = 0
    all_rows = []
    for name, fn, method in SOURCES:
        t0 = time.time()
        try:
            rows, status = fn()
        except Exception as e:                                # noqa: BLE001
            rows, status = [], f"error: {type(e).__name__}"
        new = store(conn, rows) if rows else 0
        total_new += new
        if rows:
            all_rows.extend(rows)
        # 'count' is what the database holds for this source, not what this one
        # poll happened to return. A feed that answers 200 with an empty body for
        # a moment must not zero out a source that has seven rows on disk.
        stored = conn.execute("SELECT COUNT(*) FROM incidents WHERE source=?", (name,)).fetchone()[0]
        with conn:
            conn.execute(
                """INSERT INTO sources(name,method,status,count,checked)
                   VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET
                   method=excluded.method,status=excluded.status,
                   count=excluded.count,checked=excluded.checked""",
                (name, method, status, stored,
                 datetime.now(timezone.utc).isoformat(timespec="seconds")))
        if verbose:
            print(f"  {name:22s} {status:12s} {len(rows):3d} parsed, "
                  f"{new:3d} new  ({time.time()-t0:.1f}s)")
    # The police source fans out over twenty administrations under one name; record
    # each PU as its own source row so the dashboard can show per-county freshness.
    with conn:
        for _slug, _region, label, _seat in POLICE_PUS:
            n = conn.execute("SELECT COUNT(*) FROM incidents WHERE source=?", (label,)).fetchone()[0]
            conn.execute(
                """INSERT INTO sources(name,method,status,count,checked)
                   VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET
                   count=excluded.count,checked=excluded.checked""",
                (label, "gov.hr CMS", "ok", n,
                 datetime.now(timezone.utc).isoformat(timespec="seconds")))
    # Mirror this cycle's full parse into Supabase (idempotent upsert) so a
    # published, machine-independent dashboard can read it.
    sb = push_supabase(all_rows)
    if verbose and sb != "off":
        print(f"  {'→ Supabase':22s} {sb}")
    return total_new


def poller(conn, interval, stop):
    # main() has just polled everything; wait a full interval before the first
    # background cycle rather than hitting every source twice inside ten seconds.
    stop.wait(interval)
    while not stop.is_set():
        print(f"[{datetime.now(CEST):%H:%M:%S}] polling {len(SOURCES)} sources…")
        try:
            n = poll_once(conn)
            print(f"[{datetime.now(CEST):%H:%M:%S}] {n} new incident(s) stored")
        except Exception as e:                                # noqa: BLE001
            print(f"  poll failed: {e}")
        stop.wait(interval)


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    conn = None

    def log_message(self, fmt, *args):                        # quieter console
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                                         # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            f = HERE / "dashboard.html"
            if not f.exists():
                return self._send(b"dashboard.html missing", "text/plain", 500)
            return self._send(f.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/incidents":
            now = datetime.now(CEST)
            since = (now - timedelta(days=MAX_AGE_DAYS)).strftime("%Y-%m-%d")
            # Nothing older than the max age leaves the server. The archive stays
            # in SQLite for anyone who wants it; the console is not an archive.
            rows = self.conn.execute(
                """SELECT * FROM incidents WHERE category NOT IN ('summary','weather')
                   AND date >= ? ORDER BY date DESC, time DESC LIMIT 600""", (since,)).fetchall()
            extra = self.conn.execute(
                """SELECT * FROM incidents WHERE category IN ('summary','weather')
                   AND date >= ? ORDER BY date DESC LIMIT 60""", (since,)).fetchall()
            # Freshness is the newest *event timestamp* a source has produced, not
            # whether its HTTP fetch succeeded. A missing time counts as 00:00 —
            # that errs toward dropping out of the live band, never toward faking it.
            srcs = self.conn.execute(
                """SELECT s.*, MAX(i.date || ' ' || COALESCE(NULLIF(i.time,''),'00:00')) AS freshest
                   FROM sources s LEFT JOIN incidents i
                     ON i.source = s.name
                     OR (s.name = 'Policija · 20 PU' AND i.source LIKE 'PU %')
                   GROUP BY s.name ORDER BY s.name""").fetchall()
            return self._send(json.dumps({
                "incidents": [dict(r) for r in rows],
                "context": [dict(r) for r in extra],
                "sources": [dict(r) for r in srcs],
                "served": now.isoformat(timespec="seconds"),
                "now": now.isoformat(timespec="seconds"),
                "live_hours": LIVE_WINDOW_HOURS,
                "max_age_days": MAX_AGE_DAYS,
            }, ensure_ascii=False).encode(), "application/json; charset=utf-8")
        if path == "/favicon.ico":
            return self._send(b"", "image/x-icon", 204)
        if path == "/api/refresh":
            n = poll_once(self.conn, verbose=False)
            return self._send(json.dumps({"new": n}).encode(), "application/json")
        self._send(b"not found", "text/plain", 404)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8713)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--interval", type=int, default=600,
                    help="seconds between polls (default 600; please don't go below 300)")
    ap.add_argument("--once", action="store_true", help="fetch once, report, exit")
    args = ap.parse_args()

    conn = db()
    print(f"VatroCAD · database {DB_PATH}")
    print("Initial fetch…")
    poll_once(conn)
    total = conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
    print(f"Stored incidents: {total}")
    if args.once:
        return

    stop = threading.Event()
    t = threading.Thread(target=poller, args=(conn, max(args.interval, 60), stop), daemon=True)
    t.start()

    Handler.conn = conn
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"\n  →  http://{args.host}:{args.port}   (Ctrl-C to stop)")
    print(f"     polling every {args.interval}s\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
        stop.set()


if __name__ == "__main__":
    main()
