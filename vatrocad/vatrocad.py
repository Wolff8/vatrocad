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
import hashlib
import html as htmllib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone, tzinfo
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


class _CentralEurope(tzinfo):
    """Fallback for a runner without tzdata: the EU rule (CET, CEST from the last
    Sunday of March 01:00 UTC to the last Sunday of October 01:00 UTC)."""

    @staticmethod
    def _last_sunday(year, month):
        d = datetime(year, month + 1, 1) - timedelta(days=1) if month < 12 else datetime(year, 12, 31)
        return d - timedelta(days=(d.weekday() + 1) % 7)

    def _dst(self, dt):
        if dt is None:
            return False
        start = self._last_sunday(dt.year, 3).replace(hour=2)        # 02:00 CET = 01:00 UTC
        end = self._last_sunday(dt.year, 10).replace(hour=3)         # 03:00 CEST = 01:00 UTC
        return start <= dt.replace(tzinfo=None) < end

    def utcoffset(self, dt):
        return timedelta(hours=2 if self._dst(dt) else 1)

    def dst(self, dt):
        return timedelta(hours=1 if self._dst(dt) else 0)

    def tzname(self, dt):
        return "CEST" if self._dst(dt) else "CET"


try:
    from zoneinfo import ZoneInfo
    LOCAL = ZoneInfo("Europe/Zagreb")
    datetime(2026, 1, 1, tzinfo=LOCAL).utcoffset()                 # tzdata really present?
except Exception:                                                 # noqa: BLE001
    LOCAL = _CentralEurope()
# Every source publishes wall-clock time for Croatia/Austria (one zone, DST
# included). All stamping, cut-offs and "now" go through LOCAL — a fixed +02:00
# was one hour wrong from the last Sunday of October to the last Sunday of March.
CEST = LOCAL                                                      # legacy alias

# Supabase (optional). When both are set, every poll upserts incidents into the
# hosted Postgres so a published dashboard can read them without this machine.
# SUPABASE_KEY must be the SERVICE-ROLE key (it bypasses RLS to write) — keep it
# in the environment / CI secrets, never in the dashboard, which uses the
# read-only publishable key instead.
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
SB_TIMEOUT = 15                # seconds per Supabase request (never wait out a dead DB)
SB_RETRIES = 2                 # extra attempts after the first, with back-off
SB_BACKOFF = (2, 5)
SB_CHUNK = 200                 # rows per upsert POST
SB_PUSH_MAX_AGE_DAYS = 8       # older rows are never (re)pushed — the console shows 8 days
MAX_WORKERS = 6                # concurrent source fetches (per-host serialised, see fetch)
FETCH_TIMEOUT = 20
ARTICLE_CACHE_DAYS = 30        # article bodies are immutable; keep them a month
ARTICLE_CACHE_MAX = 4000       # and never let the cache table grow past this
GEO_NEG_CACHE_HOURS = 24       # a Nominatim miss is not retried for a day

# ── Slovenian auto-translation ────────────────────────────────────────────
# Titles and narratives in these feeds are German (Austria) or Croatian; the
# console is Slovenian, so we translate them once via MyMemory's free, keyless
# endpoint (de|sl, hr|sl) and cache the result forever in the local SQLite
# (persisted between CI runs by the same actions/cache as the geocode cache).
# The original text is always kept alongside. Only rows inside the display
# window are translated — the HR archive is never shown, so never translated —
# and a per-day word budget keeps us inside the anonymous free tier; anything
# not yet translated simply shows its original until a later cycle fills it in.
MT_ENDPOINT = "https://api.mymemory.translated.net/get"
MT_DAILY_WORDS = 4000          # anonymous free tier is ~5000 words/day per IP; stay under
MT_CALLS_PER_CYCLE = 30        # bounds cycle time (~1-2 s per call), not the free tier (the word budget does that)
MT_WINDOW_DAYS = {"stmk": 8, "ktn": 8}   # AT border reports live 7d; others fall back to 4
MT_CHUNK_BYTES = 450           # MyMemory's documented limit is 500 *bytes* per query
MT_MAX_CHUNKS = 4              # a 1600-char narrative is ~4 pieces; more is not worth the calls
_mt_stop = False               # set within a cycle once the quota/budget is spent
_mt_calls = 0                  # network calls made this cycle (chunks count individually)


def _mt_words_today(conn) -> int:
    key = "mt_words_" + datetime.now(timezone.utc).strftime("%Y%m%d")
    row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return int(row["v"]) if row else 0


def _mt_add_words(conn, total: int):
    key = "mt_words_" + datetime.now(timezone.utc).strftime("%Y%m%d")
    with conn:
        conn.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=?",
                     (key, str(total), str(total)))


def mt_chunks(text: str, limit: int = MT_CHUNK_BYTES):
    """Split a narrative into sentence-aligned pieces of at most `limit` UTF-8
    bytes. A sentence longer than the limit is split at commas, then at spaces,
    so no piece ever exceeds it (Croatian diacritics are two bytes each, which is
    why the budget is in bytes, not characters)."""
    text = tidy(text or "")
    if not text:
        return []
    nbytes = lambda s: len(s.encode("utf-8"))

    def split_long(piece, seps):
        if nbytes(piece) <= limit or not seps:
            return [piece] if nbytes(piece) <= limit else _hard_split(piece, limit)
        parts, cur = [], ""
        for tok in re.split(seps[0], piece):
            tok = tok.strip()
            if not tok:
                continue
            cand = (cur + " " + tok).strip()
            if nbytes(cand) <= limit:
                cur = cand
            else:
                if cur:
                    parts.append(cur)
                cur = tok if nbytes(tok) <= limit else ""
                if not cur:
                    parts.extend(split_long(tok, seps[1:]))
        if cur:
            parts.append(cur)
        return parts

    out, cur = [], ""
    for sent in re.split(r"(?<=[.!?])\s+", text):
        for piece in split_long(sent, [r"(?<=,)\s+", r"\s+"]):
            cand = (cur + " " + piece).strip()
            if nbytes(cand) <= limit:
                cur = cand
            else:
                if cur:
                    out.append(cur)
                cur = piece
    if cur:
        out.append(cur)
    return out


def _hard_split(s: str, limit: int):
    out, cur = [], ""
    for ch in s:
        if len((cur + ch).encode("utf-8")) > limit:
            out.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def _mm_call(text: str, src: str):
    """One MyMemory request. Returns (translation|None, fatal) — fatal means the
    service or the quota is gone for this cycle; non-fatal means skip this item."""
    q = urllib.parse.urlencode({"q": text, "langpair": f"{src}|sl"})
    req = urllib.request.Request(f"{MT_ENDPOINT}?{q}", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            d = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return None, e.code in (429, 503)                        # rate limit/outage: stop; 4xx: skip
    except Exception:                                             # noqa: BLE001
        return None, True                                         # network — stop for this cycle
    out = tidy((d.get("responseData") or {}).get("translatedText") or "")
    up = out.upper()
    if d.get("quotaFinished") or "USED ALL AVAILABLE" in up or "MYMEMORY WARNING" in up:
        return None, True
    status = d.get("responseStatus")
    if not out or (isinstance(status, int) and status != 200) or "INVALID LANGUAGE PAIR" in up:
        return None, False
    time.sleep(0.4)                                              # be polite to a free service
    return out, False


def mm_translate(conn, text: str, src: str):
    """Translate `text` from `src` (de|hr) into Slovenian, cache-first. Long texts
    go over in sentence-aligned chunks of <= 450 bytes and are joined back; only
    a complete translation is cached, so a half-done narrative retries next run.
    Returns None when nothing usable is available (budget/quota spent, outage)."""
    global _mt_stop, _mt_calls
    text = tidy(text or "")
    if len(text) < 3:
        return text or None
    key = f"{src}|{hashlib.md5(text.encode()).hexdigest()}"
    row = conn.execute("SELECT v FROM translate_cache WHERE k=?", (key,)).fetchone()
    if row is not None:
        return row["v"]
    if _mt_stop:
        return None
    chunks = mt_chunks(text)[:MT_MAX_CHUNKS]
    sent = " ".join(chunks)
    words = len(sent.split())                                     # budget what is actually sent
    if _mt_words_today(conn) + words > MT_DAILY_WORDS:
        _mt_stop = True
        return None
    parts = []
    for piece in chunks:
        if _mt_calls >= MT_CALLS_PER_CYCLE:
            return None
        _mt_calls += 1
        out, fatal = _mm_call(piece, src)
        if out is None:
            if fatal:
                _mt_stop = True
            return None
        parts.append(out)
    _mt_add_words(conn, _mt_words_today(conn) + words)
    result = " ".join(parts)
    with conn:
        conn.execute("INSERT OR REPLACE INTO translate_cache(k,v) VALUES(?,?)", (key, result))
    return result


def translate_rows(conn, rows):
    """Fill each row's Slovenian title_sl / raw_sl. Austrian dispatch rows (NÖ/OÖ)
    are already glossed to Slovenian, so they cost nothing; report and Croatian
    rows go through MyMemory, newest first, only inside the display window and
    only while the day's word budget lasts. Reads the cache for rows done on an
    earlier cycle, writes new translations back to both the DB and the row dict
    (so the very same push carries them to Supabase)."""
    global _mt_stop, _mt_calls
    _mt_stop = False
    _mt_calls = 0
    ids = [r["id"] for r in rows]
    known = {}
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        qmarks = ",".join("?" * len(chunk))
        for row in conn.execute(
                f"SELECT id,title_sl,raw_sl FROM incidents WHERE id IN ({qmarks})", chunk):
            known[row["id"]] = (row["title_sl"], row["raw_sl"])
    today = datetime.now(LOCAL).date()

    def recent(r):
        try:
            d = datetime.strptime(r.get("date", ""), "%Y-%m-%d").date()
        except ValueError:
            return False
        return (today - d).days <= MT_WINDOW_DAYS.get(r.get("region"), 4)

    # Newest first; within the same day, do the few Austrian border reports
    # before the large Croatian set so the belt the user watches is never
    # starved by the HR backlog when the daily word budget runs low.
    order = sorted(rows, key=lambda r: (r.get("date", ""), r.get("country") == "AT",
                                        r.get("time", "")), reverse=True)
    for r in order:
        country, region = r.get("country", "HR"), r.get("region")
        # Weather/national-summary rows are context (KPI counts), never shown as
        # incidents — don't spend translation budget on them.
        if r.get("category") in ("summary", "weather"):
            continue
        src = "de" if country == "AT" else "hr"
        t_sl, raw_sl = known.get(r["id"], (None, None))
        # Austrian dispatch: the title is glossed Slovenian already; gloss the
        # short type string for the narrative too. No network needed.
        if country == "AT" and str(r.get("ref", "")).startswith(("NOE-", "OOE-")):
            t_sl = r.get("title"); raw_sl = at_translate(r.get("raw") or "")
        else:
            need = (t_sl is None or raw_sl is None) and recent(r) and not _mt_stop
            if need and _mt_calls < MT_CALLS_PER_CYCLE:
                if t_sl is None:
                    t_sl = mm_translate(conn, r.get("title") or "", src)
                if raw_sl is None and not _mt_stop and _mt_calls < MT_CALLS_PER_CYCLE:
                    raw_sl = mm_translate(conn, r.get("raw") or "", src)
            # No usable translation yet → leave it None. The console then shows
            # the clean original (the glossary is for dispatch type-codes and
            # would only half-translate a free-text report title into gibberish).
        r["title_sl"] = t_sl
        r["raw_sl"] = raw_sl
        if r["id"] in known and (t_sl, raw_sl) != known[r["id"]]:
            with conn:
                conn.execute("UPDATE incidents SET title_sl=?, raw_sl=? WHERE id=?",
                             (t_sl, raw_sl, r["id"]))
    return f"MT: {_mt_calls} klicev, {_mt_words_today(conn)} besed danes" + (" (kvota)" if _mt_stop else "")


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
    """Combine an event date + HH:MM into an ISO timestamp in LOCAL (CET/CEST), for ordering
    and the live window. No time on record → midnight (ages out sooner, never
    fakes freshness)."""
    if not date:
        return None
    hh, mm = (time_.split(":") + ["00", "00"])[:2] if re.match(r"^\d{1,2}:\d{2}$", time_ or "") else ("00", "00")
    try:
        return datetime(int(date[:4]), int(date[5:7]), int(date[8:10]),
                        int(hh), int(mm), tzinfo=LOCAL).isoformat()
    except (ValueError, IndexError):
        return None


def event_when(pub_date: str, pub_time: str, body_time: str):
    """Pair a call time named *inside* a report with the right calendar day.

    These sources publish after the fact: the ŽVOC morning bulletin (08.09,
    07:00) reports last night's 22:20 call, and a newsroom piece filed at 08:15
    names an hour from the small hours. Taking the publication date together
    with the body's hour therefore dates the event in the future, where the
    console rightly refuses to show it — which is how "all of Croatia" can look
    frozen for hours while fresh rows sit in the table, unshowable.

    A report cannot describe something that has not happened yet, so a body time
    later than the publication time belongs to the previous day."""
    if not body_time:
        return pub_date, pub_time
    if pub_time and body_time > pub_time:
        try:
            d = datetime.strptime(pub_date, "%Y-%m-%d") - timedelta(days=1)
            return d.strftime("%Y-%m-%d"), body_time
        except ValueError:
            pass
    return pub_date, body_time


def defuture(rows) -> int:
    """Last-resort dating guard, applied to every source. An *incident* stamped
    in the future is a parse bug, never a live call, and the dashboard hides it —
    so fix it here instead of silently losing the row. Weather warnings are
    legitimately future-dated and are left alone."""
    now = datetime.now(LOCAL)
    fixed = 0
    for r in rows:
        if r.get("category") == "weather":
            continue
        ts = to_ts(r.get("date", ""), r.get("time", ""))
        if not ts:
            continue
        if datetime.fromisoformat(ts) > now + timedelta(minutes=5):
            try:
                r["date"] = (datetime.strptime(r["date"], "%Y-%m-%d")
                             - timedelta(days=1)).strftime("%Y-%m-%d")
                fixed += 1
            except (ValueError, KeyError):
                pass
    return fixed


# ── Supabase: one guarded helper for everything ───────────────────────────
# The database was once unreachable for nine hours while the job spun on 503s
# with 25-45 s timeouts per call. Every call now has a short timeout, at most
# two retries with back-off, and a per-cycle circuit breaker: once a call has
# failed persistently, the rest of the cycle skips Supabase instantly, logs one
# line, and the poller carries on scraping — the SQLite keeps the truth, and the
# delta push catches up on the next cycle that gets through.
_SB = {"down": False, "logged": False, "has_cluster": True}


def sb_configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)


def sb_reset_cycle():
    _SB["down"] = False
    _SB["logged"] = False


def sb_call(path: str, method: str = "GET", body=None, prefer: str = "return=representation",
            timeout: int = SB_TIMEOUT, retries: int = SB_RETRIES):
    """PostgREST request with the service-role key. Returns (ok, data|error).
    4xx answers are final (bad request, missing table/column) and are returned
    to the caller; network failures, 5xx and timeouts are retried, then trip the
    breaker for this cycle. Never raises."""
    if not sb_configured():
        return False, "off"
    if _SB["down"]:
        return False, "down"
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    err = "?"
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/{path}", data=data, method=method,
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                     "Content-Type": "application/json", "Prefer": prefer})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return True, (json.loads(raw) if raw and prefer.endswith("representation") else [])
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode("utf-8", "replace")
            err = f"HTTP {e.code} {detail}"
            if e.code < 500 and e.code != 429:
                return False, err                                 # final answer, not an outage
        except Exception as e:                                    # noqa: BLE001
            err = type(e).__name__
        if attempt < retries:
            time.sleep(SB_BACKOFF[min(attempt, len(SB_BACKOFF) - 1)])
    _SB["down"] = True
    if not _SB["logged"]:
        _SB["logged"] = True
        print(f"  Supabase ne odgovarja ({err}) — nadaljujem brez sinhronizacije v tem ciklu")
    return False, err


def control_read():
    """Current control row, or None when Supabase is unconfigured/unreachable."""
    ok, rows = sb_call("control?id=eq.1&select=*", timeout=10)
    return rows[0] if ok and rows else None


def control_write(**fields) -> bool:
    if not fields or not sb_configured():
        return True                                               # nothing to write to
    ok, _ = sb_call("control?id=eq.1", "PATCH", fields, prefer="return=minimal", timeout=10)
    return ok


def serve_loop(conn, minutes: int, interval: int = 600, verbose: bool = True):
    """Run as a long-lived poller for `minutes`, then exit.

    Why this exists: GitHub's cron is best-effort and was dropping most of the
    */15 slots — the console looked frozen for hours because nothing was
    scraping, not because the sources were quiet. One hourly job that stays up
    and polls on its own clock is honoured reliably, and while it is up the
    dashboard can ask for an immediate poll by stamping control.poll_requested_at
    (public.request_poll()), which we notice within ~15 seconds. No credentials
    ever leave the runner for that: the page only stamps a request.

    Cadence: the FAST tier runs every cycle, the SLOW tier every second cycle.
    A manual request and the first cycle of a job always run everything; the
    first cycle also re-pushes every row (self-healing against anything a
    previous outage left behind)."""
    deadline = time.time() + minutes * 60
    handled = None                     # newest request stamp already acted on
    last_poll = 0.0
    cycle = 0
    ctl_failures = 0
    while True:
        sb_reset_cycle()
        row = control_read()
        ctl_failures = 0 if (row is not None or not sb_configured()) else ctl_failures + 1
        req = (row or {}).get("poll_requested_at")
        manual = bool(req and req != handled and time.time() - last_poll > 45)
        due = time.time() - last_poll >= interval
        if manual or last_poll == 0.0 or due:
            first = last_poll == 0.0
            full = manual or first or cycle % 2 == 0
            control_write(poll_started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          runner_until=datetime.fromtimestamp(deadline, timezone.utc).isoformat(timespec="seconds"),
                          note="pobiram s virov…")
            why = "manual" if manual else ("first" if first else "scheduled")
            if verbose:
                print(f"[{datetime.now(LOCAL):%H:%M:%S}] poll ({why}, {'vsi viri' if full else 'hitri viri'})")
            try:
                res = poll_once(conn, verbose=verbose, full=full, resync=first)
            except Exception as e:                                # noqa: BLE001
                res = {"new": 0, "push_ok": False, "error": f"{type(e).__name__}: {e}"}
                print(f"  poll failed: {res['error']}")
            last_poll = time.time()
            cycle += 1
            if req:
                handled = req          # honoured (or superseded) whatever the reason for polling
            n = res.get("new", 0)
            if res.get("push_ok"):
                control_write(poll_finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                              note=f"{n} novih")
            elif res.get("error"):
                control_write(note=f"napaka pobiralnika: {res['error'][:80]}")
            else:
                # Never stamp a finish the data does not back: the dashboard's
                # freshness comes from poll_finished_at.
                control_write(note="Supabase ne odgovarja – podatki niso osveženi")
            if verbose:
                print(f"[{datetime.now(LOCAL):%H:%M:%S}] {n} new; next in {interval//60} min "
                      f"or on request ({(deadline-time.time())/60:.0f} min of runtime left)")
        if time.time() >= deadline:
            control_write(note="pobiralnik miruje – naslednji zagon ob polni uri", runner_until=None)
            if verbose:
                print("serve window over — exiting so the next hourly run takes over")
            return
        # Read the control row every 15 s while Supabase answers; back off to a
        # minute once it has failed twice in a row (no point hammering an outage).
        pause = 60 if ctl_failures >= 2 else 15
        time.sleep(min(pause, max(1, deadline - time.time())))


SB_COLUMNS = ("id", "source", "region", "country", "ref", "occurred", "occurred_time", "ts",
              "category", "status", "title", "location", "lat", "lon", "units", "crew",
              "vehicles", "raw", "link", "title_sl", "raw_sl", "cluster")


def sb_payload(r: dict) -> dict:
    """The Supabase row for a parsed incident (without last_seen)."""
    blob = f"{r.get('title', '')} {r.get('raw', '')}"
    return {
        "id": r["id"], "source": r["source"], "region": r.get("region"),
        "country": r.get("country", "HR"),
        "ref": r.get("ref"), "occurred": r.get("date") or None,
        "occurred_time": r.get("time") or None, "ts": to_ts(r.get("date", ""), r.get("time", "")),
        # A source that knows its own status (Austria's dispatch pages do)
        # passes it explicitly; otherwise derive it from Croatian verbs.
        "category": r.get("category"), "status": r.get("status") or status_of(blob),
        "title": r.get("title"), "location": r.get("location"),
        "lat": r.get("lat"), "lon": r.get("lon"), "units": r.get("units") or None,
        "crew": r.get("crew"), "vehicles": r.get("vehicles"),
        "raw": r.get("raw"), "link": r.get("link"),
        "title_sl": r.get("title_sl"), "raw_sl": r.get("raw_sl"),
        "cluster": r.get("cluster"),
    }


def content_hash(payload: dict) -> str:
    """Stable digest of what the dashboard would see — key order and last_seen
    play no part, so an unchanged row hashes the same on every cycle."""
    body = {k: v for k, v in payload.items() if k != "last_seen"}
    return hashlib.md5(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                  separators=(",", ":")).encode("utf-8")).hexdigest()


def push_supabase(conn, rows, resync: bool = False):
    """Delta upsert into Supabase. Only rows that are new or whose content hash
    changed since the last successful push go over, plus NÖ LIVE rows (their
    last_seen drives the live prune) — and everything once per job (`resync`)
    so a lost write heals itself. Rows older than SB_PUSH_MAX_AGE_DAYS are never
    sent: the dashboard shows eight days and the HR archive lives in SQLite.
    Sent in chunks; one failed chunk never stops the others. Returns
    (summary, ok) — ok is False only when a chunk could not be written."""
    if not sb_configured():
        return "off", True
    now_utc = datetime.now(timezone.utc)
    now = now_utc.isoformat(timespec="seconds")
    min_date = (now_utc.astimezone(LOCAL) - timedelta(days=SB_PUSH_MAX_AGE_DAYS)).strftime("%Y-%m-%d")
    # PostgREST turns one POST into a single INSERT ... ON CONFLICT DO UPDATE, and
    # Postgres refuses to touch the same conflict target twice in one statement
    # (error 21000) — so if two parsed rows in this cycle share an id (an
    # overlapping paginated source, or two sources hashing to the same id), the
    # WHOLE batch is rejected and nothing is written, silently. Dedup by id first
    # (last one wins) so one collision can never take the rest of the poll down.
    by_id = {}
    for r in rows:
        by_id[r["id"]] = r
    # Rows that never made it over (stored or changed during an outage carry
    # hash NULL) must go even when their source answered 304 this cycle and
    # emitted nothing — otherwise they would wait for the next content change.
    pending = 0
    for row in conn.execute("SELECT * FROM incidents WHERE hash IS NULL AND date >= ?", (min_date,)):
        if row["id"] not in by_id:
            by_id[row["id"]] = dict(row)
            pending += 1
    ids = list(by_id)
    stored = {}
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        for row in conn.execute(f"SELECT id, hash FROM incidents WHERE id IN ({','.join('?' * len(chunk))})", chunk):
            stored[row["id"]] = row["hash"]
    todo, skipped_old, unchanged = [], 0, 0
    for r in by_id.values():
        p = sb_payload(r)
        if p["occurred"] and p["occurred"] < min_date:
            skipped_old += 1
            continue
        h = content_hash(p)
        live = str(p.get("ref") or "").startswith("NOE-LIVE")
        if not (resync or live or stored.get(r["id"]) != h):
            unchanged += 1
            continue
        p["last_seen"] = now
        todo.append((p, h))
    if not todo:
        return f"nič novega ({unchanged} nespremenjenih, {skipped_old} starejših od {SB_PUSH_MAX_AGE_DAYS} dni)", True
    sent, rejected, errors = 0, 0, []
    outage = False
    for i in range(0, len(todo), SB_CHUNK):
        if _SB["down"]:
            outage = True
            break
        s, rj, errs = _push_chunk(conn, todo[i:i + SB_CHUNK])
        sent += s
        rejected += rj
        errors.extend(errs)
    if _SB["down"]:
        outage = True
    summary = (f"poslanih {sent}"
               + (f", {rejected} zavrnjenih ({'; '.join(errors[:2])})" if rejected else "")
               + (f", izpad ({len(todo) - sent - rejected} neposlanih)" if outage else "")
               + f", {unchanged} nespremenjenih, {skipped_old} starejših od {SB_PUSH_MAX_AGE_DAYS} dni"
               + (", polna sinhronizacija" if resync else "")
               + (f", {pending} iz prejšnjega izpada" if pending else ""))
    # Nothing written at all (every chunk refused) is not a good cycle either.
    return summary, not outage and not (rejected and not sent)


def _push_chunk(conn, chunk):
    """Upsert one chunk of (payload, hash). A chunk PostgREST rejects outright
    (4xx: one malformed row poisons the whole INSERT) is bisected so only the
    offending row is lost; an outage trips the breaker and stops. Returns
    (sent, rejected, errors)."""
    payload = [p for p, _h in chunk]
    if not _SB["has_cluster"]:
        for p in payload:
            p.pop("cluster", None)
    ok, err = sb_call("incidents?on_conflict=id", "POST", payload,
                      prefer="resolution=merge-duplicates,return=minimal", timeout=SB_TIMEOUT + 5)
    if not ok and "cluster" in str(err) and _SB["has_cluster"]:
        # The table predates the cluster column — send without it from now on.
        _SB["has_cluster"] = False
        return _push_chunk(conn, chunk)
    if ok:
        with conn:
            conn.executemany("UPDATE incidents SET hash=? WHERE id=?", [(h, p["id"]) for p, h in chunk])
        return len(chunk), 0, []
    if _SB["down"] or err == "down":
        return 0, 0, []
    # A systemic refusal (expired key, RLS, missing table/column) fails every
    # sub-chunk identically — bisecting it would cost 2n-1 requests per cycle.
    systemic = re.search(r"HTTP (401|403|404)|PGRST204|PGRST301|PGRST30", str(err))
    if len(chunk) == 1 or systemic:
        return 0, len(chunk), [f"{chunk[0][0]['id']}: {str(err)[:120]}"]
    half = len(chunk) // 2
    a = _push_chunk(conn, chunk[:half])
    if half >= 4 and a[0] == 0 and a[1] == half and a[2] and str(err)[:60] in a[2][0]:
        return 0, len(chunk), a[2]          # the whole half failed the same way: don't split the rest
    b = _push_chunk(conn, chunk[half:])
    return a[0] + b[0], a[1] + b[1], a[2] + b[2]


# Sources whose rows the console treats as a rolling 3-day dispatch log. Only
# these are pruned early; police and after-action report rows live 7 days.
AT_DISPATCH_SOURCES = ("NÖ Feuerwehr · Wastl", "OÖ Feuerwehr · LFV")


def prune_supabase_austria(days=MAX_AGE_DAYS):
    """Austria's retention rule is stricter than Croatia's: actually DELETE
    rows older than `days`, not just hide them client-side. Croatia's archive
    stays forever by design (the README documents that intentionally); this
    is a deliberately different, narrower rule scoped to country=AT only.

    The 3-day rule applies to the two dispatch logs only. Police and report
    rows are emitted with a 7-day window by their sources, so pruning them at
    3 days would delete and re-insert them every cycle (and ring the console's
    new-incident toast each time). Never raises."""
    if not sb_configured():
        return "off"
    now = datetime.now(LOCAL)
    cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    report_cutoff = (now - timedelta(days=AT_REPORT_MAX_AGE_DAYS)).strftime("%Y-%m-%d")
    stale_live = (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat(timespec="seconds")
    quoted = ",".join('"' + s + '"' for s in AT_DISPATCH_SOURCES)
    results = []
    for label, path in (
        # Age-out: the two live dispatch logs past the 3-day window …
        ("dispatch", urllib.parse.quote(f"incidents?country=eq.AT&source=in.({quoted})&occurred=lt.{cutoff}",
                                        safe="=&().,?")),
        # … and every Austrian row (police, after-action reports) past 7 days.
        ("reports", f"incidents?country=eq.AT&occurred=lt.{report_cutoff}"),
        # NÖ "running" rows are re-pushed (last_seen refreshed) every poll for
        # as long as the call is open; one not re-seen for 40 minutes has
        # closed and its exact-time history row has replaced it.
        ("live", f"incidents?country=eq.AT&ref=like.NOE-LIVE*&last_seen=lt.{urllib.parse.quote(stale_live)}"),
    ):
        ok, err = sb_call(path, "DELETE", prefer="return=minimal")
        results.append(f"{label} ok" if ok else f"{label} napaka {str(err)[:80]}")
        if not ok and err == "down":
            break
    return "pruned (" + ", ".join(results) + ")"


def push_source_status(statuses):
    """One row per source in public.source_status (source, ok, rows, ms, error,
    checked_at). The table is optional: a 404 (not created yet) is ignored."""
    if not sb_configured() or not statuses:
        return "off"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = [{"source": name, "ok": st in ("ok", "unchanged"), "rows": int(n), "ms": int(ms),
                "error": None if st in ("ok", "unchanged") else str(st)[:200], "checked_at": now}
               for name, st, n, ms in statuses]
    ok, err = sb_call("source_status?on_conflict=source", "POST", payload,
                      prefer="resolution=merge-duplicates,return=minimal")
    if ok:
        return f"{len(payload)} virov"
    if str(err).startswith("HTTP 404"):
        return "tabela source_status ne obstaja"
    return f"napaka {str(err)[:80]}"

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
    "garčin": (45.164, 18.223), "valpovo": (45.659, 18.417), "nerežišća": (43.331, 16.581),
    "pučišća": (43.348, 16.727), "tenja": (45.497, 18.747),
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
               "tjedna analiza", "sažetak", "pregled događaja", "uhićen", "provala", "provaljen",
               "otuđ", "krađ", "prijevar", "droga", "kazneno djelo", "remeti", "glazb", "alkohol",
               "najava", "pomorsk")
# Crime that is not an emergency call-out. Used to filter media crime sections.
CRIME_SKIP = ("provala", "provaljen", "krađ", "otuđ", "droga", "uhićen", "prijevar", "nasilj",
              "prijetnj", "tučnjav", "remeti", "kazneno djelo", "razbojni", "pretres")
# Administrative/enforcement stories that share vocabulary with real
# interventions but aren't one: a roadworthiness order, a court fine, a licence
# suspension — police-blotter admin, not a dispatched call. Headline-only: the
# same words are routine inside the body of a real DUI crash report.
ENFORCEMENT_SKIP = ("prekršaj", "izvanredni tehnički", "oduzeo vozačku", "trajno oduzet",
                    "zabranom vožnje", "zabranom upravljanja", "novčano kažnjen", "novčanom kaznom",
                    "neisprav", "isključili iz prometa", "isključio iz prometa",
                    "isključen iz prometa", "isključili iz promet")


def blotter_skip(title: str, body: str = "") -> bool:
    """True for a crime/enforcement story that is not a call-out. Decided on the
    headline: a headline that is itself a crash, fire or rescue is never dropped,
    however much police phrasing the body carries ('vozač isključen iz prometa'
    is in every DUI-crash report). Only when the headline names no incident at
    all does the body's crime vocabulary get a say."""
    tl = title.lower()
    if categorise(title, title) in ("accident", "fire", "rescue"):
        return False
    if any(k in tl for k in CRIME_SKIP) or any(k in tl for k in ENFORCEMENT_SKIP):
        return True
    return _cat(tl) is None and any(k in body.lower() for k in CRIME_SKIP)


def cut_title(s: str, limit: int = 110) -> str:
    """Trim a headline at a word boundary with an ellipsis — never mid-word."""
    s = tidy(s or "")
    if len(s) <= limit:
        return s
    cut = s[:limit - 1]
    if " " in cut[limit // 2:]:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,;:-–") + "…"


_LOWER_WORDS = {"na", "u", "ob", "pri", "od", "do", "kod", "i", "an", "der", "am", "im", "bei",
                "in", "ob", "an der", "pod", "nad", "za"}


def place_case(name: str) -> str:
    """Title-case a place name the way maps write it: 'Biograd na Moru',
    'Sveti Martin na Muri', 'Neumarkt an der Raab' — connectives stay lower."""
    words = (name or "").split()
    out = []
    for i, w in enumerate(words):
        lw = w.lower()
        out.append(lw if (i > 0 and lw in _LOWER_WORDS) else (w[:1].upper() + w[1:].lower()))
    return " ".join(out)

MONTHS_HR = {"siječnja": 1, "veljače": 2, "ožujka": 3, "travnja": 4, "svibnja": 5, "lipnja": 6,
             "srpnja": 7, "kolovoza": 8, "rujna": 9, "listopada": 10, "studenoga": 11,
             "studenog": 11, "prosinca": 12}

CATEGORY_RULES = [
    # Police-reported casualty events. Traffic accidents with injuries are the
    # single largest driver of EMS call-outs, so they belong in a fire+hitna view,
    # but they get their own category so they never masquerade as brigade calls.
    ("accident", ("prometna nesreća", "prometne nesreće", "prometnoj nesreći", "prometnu nesreću",
                  "prometnih nesreća", "poginu", "smrtno stradal", "teško ozlijeđ", "tesko ozlijed",
                  "lakše ozlijeđ", "nastradal", "utopi", "eksplozij", "pad s visine", "ozlijeđen",
                  # declined/colloquial crash phrasing police headlines actually use
                  "udario u", "udarila u", "udar u ", "naletio", "naletjel", "sletio", "sletjel",
                  "slijetanj", "prevrnu", "sudar", "skrivio nesreću", "skrivila nesreću",
                  "pod kotač", "pregaz", "srušio se", "pao s ")),
    # Search-and-rescue and body finds: police report them, no one else does.
    ("rescue", ("nestala osoba", "nestalog", "nestale", "potrag", "spašen", "spasili", "spašavanj",
                "spasavanj", "pronađeno tijelo", "pronađen mrtav", "pronađena mrtva", "beživotno")),
    ("ems",   ("asistencij", "hmp", "hitne medicinske", "bolesne osobe", "sanitetsk")),
    ("fire",  ("požar", "pozar", "vatrodojav", "užaren", "uzaren", "dim ", "gorenj", "zapalj")),
    ("tech",  ("tehnič", "tehnic", "ispumpav", "crpljen", "saniranj", "krovišt", "prometn nesrec",
               "otvaranje vrata", "spašavanj", "spasavanj")),
]


ROUNDUP = ("vikend", "evidentiran", "tijekom protekl", "prometne nesreće i prekršaji",
           "tjedni pregled", "u proteklih", "u protekla")
_TALLY_RE = re.compile(
    r"(?:\b\d+|\bdvije|\btri|\bčetiri|\bpet|\bšest|\bsedam|\bosam|\bdevet|\bdeset)\s+"
    r"(?:prometn|požar|intervencij|događaj|nesreć|osob)"
    r"|tijekom\s+(?:protekl|vikend)|u\s+protekl|tjedni\s+pregled|pregled\s+događaja"
    r"|prometne nesreće i prekršaji")


def is_roundup(title: str) -> bool:
    """A tally over a period ('Tijekom vikenda 12 prometnih nesreća'), not one
    call. 'Evidentirana prometna nesreća s ozlijeđenom osobom' has a roundup
    word but the shape of a single incident — it is not a summary."""
    tl = title.lower()
    if not any(k in tl for k in ROUNDUP):
        return False
    if re.search(r"\bnesreć[aiu]\s+s\b|\bnesreći\b", tl) and not re.search(r"\b\d+\s+prometn", tl):
        return False
    return bool(_TALLY_RE.search(tl))


def _cat(low: str):
    for cat, keys in CATEGORY_RULES:
        if any(k in low for k in keys):
            return cat
    return None


def categorise(text: str, title: str = "") -> str:
    """Headlines are cleaner than bodies. A body that says 'nobody was hurt' still
    contains the word for 'hurt', so the title decides whenever it can."""
    tl = title.lower()
    if tl and is_roundup(title):
        return "summary"                         # a weekend tally, not one call
    return _cat(tl) or _cat(text.lower()) or "other"


UNIT_RE = re.compile(r"\b(?:JVP|DVD|IVP|VZ\w*|HGSS)\s+[A-ZŠĐČĆŽ][\wšđčćž]*(?:\s*[–-]\s*\w+)?", re.U)
# "na području Perkovića", "u gradskom predjelu Ražine", "u Ulici X" — the phrases
# that actually name where the call was, as opposed to which unit answered it.
LOC_PHRASE = re.compile(
    # "na Braču", "s Brača", "na otoku Hvaru": islands and uplands take na/s
    # rather than u, so those count as location phrases too. "iz" does not —
    # it usually says where a crew came from, not where the call is.
    r"(?i:na\s+području|u\s+gradskom\s+predjelu|u\s+naselju|na\s+otoku|kod|između|u|na|sa?)\s+"
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
    # Capitalised as in prose: several gazetteer names are also common nouns
    # ('rijeka' is a river, 'bol' is pain, 'luka' a harbour, 'zaton' a cove),
    # and a headline about Brač was landing in Rijeka because its body said
    # "rijeka" in the plain sense. Croatian capitalises place names, so the
    # first letter must be upper case; the rest matches case-insensitively.
    best = None
    for name, coords in PLACES.items():
        pat = (r"(?<![\wšđčćž])" + re.escape(name[0].upper()) + "(?i:" + re.escape(name[1:]) + ")"
               + r"(?![\wšđčćž])")
        if re.search(pat, scrubbed) and (best is None or len(name) > len(best[0])):
            best = (name, coords)
    return best[1] if best else (None, None)


# --------------------------------------------------------------------------
# Street-level refinement, on top of the settlement-centroid gazetteer above.
# The gazetteer only ever gets an incident to the right town — good enough for
# a village brigade, but "u ulici Bana Ivana Mažuranića u Šibeniku" names an
# actual street the gazetteer can't resolve on its own. Nominatim (OSM's free
# geocoder) can, so a text that names a street gets a second, sharper lookup.
# Strictly rate-limited and cached forever locally, per Nominatim's usage
# policy (max 1 request/second, an identifying User-Agent, no bulk geocoding) —
# this only ever geocodes the handful of *new* street names a cycle's parse
# actually contains, never the whole archive. Best-effort throughout: any
# failure just leaves the existing settlement-centroid fix in place.
# --------------------------------------------------------------------------
STREET_RE = re.compile(
    # A Croatian street name is not always led by a capitalised word — "Ulica
    # kralja Tomislava" starts with a lowercase title noun — so anchor only on
    # "ulic(a/i/u/e)" itself, not on capitalisation of what follows.
    r"\b[Uu]lic\w*\s+([\wšđčćžA-ZŠĐČĆŽ][\wšđčćžA-ZŠĐČĆŽ.\- ]{2,40}?)(?=\s+u\s|[,.]|$)", re.U)
NOMINATIM_UA = "VatroCAD/1.0 (personal dispatch dashboard; +https://github.com/Wolff8/vatrocad)"
NOMINATIM_MAX_PER_CYCLE = int(os.getenv("NOMINATIM_BUDGET", "25"))   # shared by HR streets + AT towns
_nominatim_last_call = [0.0]
_NOMINATIM_LOCK = threading.Lock()     # one request in flight, 1.1 s apart, whatever the thread
_geo_budget = [NOMINATIM_MAX_PER_CYCLE]  # remaining lookups this cycle (reset in poll_once)


def _nominatim_lookup(query: str, countrycodes: str = "hr"):
    """One rate-limited Nominatim call. Never raises; returns (lat, lon) or None.
    `countrycodes` defaults to Croatia (the original street-refinement use);
    Austrian callers pass "at" — without this, a Croatia-only filter silently
    empties every Austrian result rather than erroring, which is exactly what
    happened on first wiring this up: geocoding "succeeded" with None every
    time, no error, no hint why. Debits the shared per-cycle budget; returns
    None without calling once it is spent."""
    with _NOMINATIM_LOCK:
        if _geo_budget[0] <= 0:
            # Distinct from a miss: a spent budget must never be cached as
            # "town not found" (that locked hundreds of towns out for a day).
            return "budget"
        _geo_budget[0] -= 1
        wait = _nominatim_last_call[0] + 1.1 - time.time()
        if wait > 0:
            time.sleep(wait)
        _nominatim_last_call[0] = time.time()
        url = (f"https://nominatim.openstreetmap.org/search?format=json&limit=1"
               f"&countrycodes={countrycodes}&q=" + urllib.parse.quote(query))
        req = urllib.request.Request(url, headers={"User-Agent": NOMINATIM_UA})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            if data:
                return float(data[0]["lat"]), float(data[0]["lon"])
        except Exception:                                         # noqa: BLE001
            return "error"
        return None


def _geo_cached(conn, key: str):
    """(found, lat, lon): a hit, a fresh miss (found with None coordinates), or
    not in the cache / a miss old enough to retry."""
    row = conn.execute("SELECT lat, lon, tried_at FROM geocode_cache WHERE q=?", (key,)).fetchone()
    if row is None:
        return False, None, None
    if row["lat"] is not None:
        return True, row["lat"], row["lon"]
    tried = row["tried_at"] or ""
    fresh = tried >= (datetime.now(timezone.utc) - timedelta(hours=GEO_NEG_CACHE_HOURS)).isoformat(timespec="seconds")
    return (True, None, None) if fresh else (False, None, None)


def _geo_remember(conn, key: str, lat, lon):
    """Cache a hit forever, a miss for GEO_NEG_CACHE_HOURS (a typo town was
    otherwise re-queried every poll — against Nominatim's 'no repeated
    identical queries' rule)."""
    with conn:
        conn.execute("INSERT OR REPLACE INTO geocode_cache(q, lat, lon, tried_at) VALUES(?,?,?,?)",
                     (key, lat, lon, datetime.now(timezone.utc).isoformat(timespec="seconds")))


def refine_locations(conn, rows):
    """Sharpen a settlement-centroid fix to an actual street, for incidents
    whose text names one. Mutates lat/lon on `rows` in place."""
    for r in rows:
        # No early exit on a spent budget: a cached street fix must still be
        # applied, or a row's coordinates would flip between cycles depending
        # on how much budget the Austrian towns happened to leave.
        if r.get("lat") is None or not r.get("location"):
            continue
        blob = f"{r.get('title', '')} {r.get('raw', '')}"
        m = STREET_RE.search(blob)
        if not m:
            continue
        street = tidy(m.group(1))
        if len(street) < 4:
            continue
        cache_key = f"{street}|{r['location']}"
        found, lat, lon = _geo_cached(conn, cache_key)
        if not found:
            if _geo_budget[0] <= 0:
                continue                                   # only the network lookup needs budget
            hit = _nominatim_lookup(f"{street}, {r['location']}, Hrvatska")
            if hit in ("error", "budget"):
                continue                                   # transient: not a miss, retry next cycle
            lat, lon = hit if hit else (None, None)
            _geo_remember(conn, cache_key, lat, lon)
        if lat is None:
            continue
        # A street "hit" far from the settlement centroid is a bad match (a
        # same-named street in a different town) — keep the safer fallback.
        if abs(lat - r["lat"]) < 0.35 and abs(lon - r["lon"]) < 0.5:
            r["lat"], r["lon"] = lat, lon


# --------------------------------------------------------------------------
# HTTP with conditional GET, so repeat polls cost the source almost nothing.
# Sources are fetched concurrently (poll_once), but never two requests to the
# same host at once — a per-host lock keeps every volunteer server at the
# load of a single polite client. ETag/Last-Modified validators live in
# SQLite so a conditional GET works on the first cycle of every job, too.
# --------------------------------------------------------------------------
_CACHE_VALIDATORS = {}
_VALIDATORS_LOCK = threading.Lock()
_HOST_LOCKS = {}
_HOST_LOCKS_GUARD = threading.Lock()
_CYCLE = {"resync": False}     # True on a job's first cycle: skip validators, re-read everything
_thread_db = threading.local()


def cache_db():
    """A per-thread SQLite connection to the same file for the caches that the
    concurrently running source fetchers touch (geocode, article, validators,
    NÖ live starts). SQLite serialises the writes itself; the busy timeout
    covers the brief contention."""
    conn = getattr(_thread_db, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        _thread_db.conn = conn
    return conn


def _host_lock(url: str):
    host = urllib.parse.urlsplit(url).hostname or url
    with _HOST_LOCKS_GUARD:
        lock = _HOST_LOCKS.get(host)
        if lock is None:
            lock = _HOST_LOCKS[host] = threading.Lock()
    return lock


def load_validators(conn):
    with _VALIDATORS_LOCK:
        for row in conn.execute("SELECT url, etag, modified FROM http_validators"):
            _CACHE_VALIDATORS[row["url"]] = {"etag": row["etag"], "modified": row["modified"]}


def _save_validator(url: str, etag, modified):
    with _VALIDATORS_LOCK:
        old = _CACHE_VALIDATORS.get(url)
        _CACHE_VALIDATORS[url] = {"etag": etag, "modified": modified}
        if old == _CACHE_VALIDATORS[url]:
            return
    try:
        conn = cache_db()
        with conn:
            if etag or modified:
                conn.execute("INSERT OR REPLACE INTO http_validators(url, etag, modified, saved_at) VALUES(?,?,?,?)",
                             (url, etag, modified, datetime.now(timezone.utc).isoformat(timespec="seconds")))
            else:
                conn.execute("DELETE FROM http_validators WHERE url=?", (url,))
    except sqlite3.Error:
        pass


def fetch(url: str, timeout: int = FETCH_TIMEOUT, conditional: bool = True):
    """Return (text, status). status is 'ok', 'unchanged', or 'error: ...'."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "hr,en;q=0.8",
    })
    if conditional and not _CYCLE["resync"]:
        with _VALIDATORS_LOCK:
            val = dict(_CACHE_VALIDATORS.get(url, {}))
        if val.get("etag"):
            req.add_header("If-None-Match", val["etag"])
        if val.get("modified"):
            req.add_header("If-Modified-Since", val["modified"])
    # One retry on a *network* failure (reset tunnel, DNS blip, timeout): these
    # are transient and were costing a source a whole cycle. HTTP errors are
    # answered by the server and are not retried.
    with _host_lock(url):
        for attempt in (1, 2):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read()
                    _save_validator(url, resp.headers.get("ETag"), resp.headers.get("Last-Modified"))
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
            except Exception as e:                            # noqa: BLE001
                if attempt == 1:
                    time.sleep(2)
                    continue
                return None, f"error: {type(e).__name__}"


_article_budget = {}           # budget_key -> uncached fetches this cycle (reset in poll_once)
_ARTICLE_LOCK = threading.Lock()


def fetch_article(url: str, reduce, budget_key: str = "", budget: int = 6):
    """An article body, fetched once ever. `reduce(html) -> str|None` turns the
    page into the text the caller keeps (None = page not usable, not cached).
    Articles never change, so the reduced text is cached in SQLite for
    ARTICLE_CACHE_DAYS; per `budget_key` at most `budget` uncached pages are
    fetched per cycle so a cold cache cannot stall a poll. Returns the text or
    None."""
    conn = cache_db()
    row = conn.execute("SELECT body FROM article_cache WHERE url=?", (url,)).fetchone()
    if row is not None:
        return row["body"]
    with _ARTICLE_LOCK:
        if _article_budget.get(budget_key, 0) >= budget:
            return None
        _article_budget[budget_key] = _article_budget.get(budget_key, 0) + 1
    html, _st = fetch(url, conditional=False)
    if not html:
        return None
    text = reduce(html)
    if text is None:
        return None
    with conn:
        conn.execute("INSERT OR REPLACE INTO article_cache(url, fetched_at, body) VALUES(?,?,?)",
                     (url, datetime.now(timezone.utc).isoformat(timespec="seconds"), text))
    return text


def evict_caches(conn, verbose: bool = True):
    """Housekeeping once per cycle: expire article bodies, cap the table, drop
    stale negative geocode entries, old validators and old NÖ live starts."""
    now = datetime.now(timezone.utc)
    msgs = []
    with conn:
        n = conn.execute("DELETE FROM article_cache WHERE fetched_at < ?",
                         ((now - timedelta(days=ARTICLE_CACHE_DAYS)).isoformat(timespec="seconds"),)).rowcount
        if n:
            msgs.append(f"{n} člankov starejših od {ARTICLE_CACHE_DAYS} dni")
        total = conn.execute("SELECT COUNT(*) FROM article_cache").fetchone()[0]
        if total > ARTICLE_CACHE_MAX:
            n = conn.execute("""DELETE FROM article_cache WHERE url IN (
                                  SELECT url FROM article_cache ORDER BY fetched_at LIMIT ?)""",
                             (total - ARTICLE_CACHE_MAX,)).rowcount
            msgs.append(f"{n} najstarejših člankov (omejitev {ARTICLE_CACHE_MAX})")
        n = conn.execute("DELETE FROM geocode_cache WHERE lat IS NULL AND (tried_at IS NULL OR tried_at < ?)",
                         ((now - timedelta(hours=GEO_NEG_CACHE_HOURS)).isoformat(timespec="seconds"),)).rowcount
        if n:
            msgs.append(f"{n} zastarelih negativnih geokod")
        n = conn.execute("DELETE FROM http_validators WHERE saved_at < ?",
                         ((now - timedelta(days=30)).isoformat(timespec="seconds"),)).rowcount
        if n:
            msgs.append(f"{n} validatorjev")
        n = conn.execute("DELETE FROM live_start WHERE seen_at < ?",
                         ((now - timedelta(days=3)).isoformat(timespec="seconds"),)).rowcount
        if n:
            msgs.append(f"{n} NÖ live začetkov")
    if msgs and verbose:
        print("  cache: odstranjeno " + ", ".join(msgs))


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
            "raw": body[:1600], "link": p.get("link", ""),
        })
    return out, status


NEWS_ITEM = re.compile(
    r"<div class='news_item'><a href='([^']+)'><span class='h3'>(.*?)</span></a>(.*?)"
    r"<span class='date'>(\d{2})\.(\d{2})\.(\d{4})\.", re.S)


def _event_date(text: str, published: str) -> str:
    """Police write the event date in prose ('U petak, 4. rujna oko 3:20'). Prefer
    it over the publish date, which can trail the event by a day or two."""
    try:
        pub = datetime.strptime(published, "%Y-%m-%d")
    except ValueError:
        return published
    # Police prose often opens with a period ('Od 1. siječnja do danas…')
    # before naming the call, so every date in the text is a candidate and the
    # latest one within the last ten days wins; anything older is context, not
    # the event.
    best = None
    for m in re.finditer(r"(\d{1,2})\.\s*(" + "|".join(MONTHS_HR) + r")(?:\s*(\d{4}))?", text):
        year = m.group(3) or published[:4]
        try:
            d = datetime(int(year), MONTHS_HR[m.group(2)], int(m.group(1)))
        except ValueError:
            continue
        if d > pub:
            # Far ahead means the year rolled over ('28. prosinca' read in
            # January). A few days ahead is an announcement or a deadline
            # ('do 10. rujna') — not the event.
            if (d - pub).days > 300:
                d = d.replace(year=d.year - 1)
            else:
                continue
        if (pub - d).days <= 10 and (best is None or d > best):
            best = d
    return best.strftime("%Y-%m-%d") if best else published


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
    for pu in POLICE_PUS:
        rows, status = src_police_one(*pu)
        out.extend(rows)
        if status not in ("ok", "unchanged"):
            worst = status
    return out, worst


def _gov_article_text(html: str):
    plain = tidy(strip_tags(re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", html)))
    return plain or None


def src_police_one(slug: str, region: str, label: str, seat: str):
    """One county police administration. Article bodies come from the article
    cache (fetched once ever), so a cycle costs one listing request plus the
    handful of articles that are genuinely new."""
    out = []
    base = f"https://{slug}-policija.gov.hr"
    cutoff = (datetime.now(LOCAL) - timedelta(days=LIVE_WINDOW_DAYS + 1)).strftime("%Y-%m-%d")
    listing, status = fetch(f"{base}/vijesti/8")
    if listing is None:
        return out, status
    for href, title_html, excerpt_html, dd, mm, yy in NEWS_ITEM.findall(listing):
        title = tidy(strip_tags(title_html))
        low = title.lower()
        tcat = categorise(title, title)
        # PR/enforcement headlines are dropped — unless the headline is itself
        # a crash or a rescue ("s 2,31 promila skrivio nesreću", "akcija
        # potrage"): the skip word is then incidental to a real call-out.
        if any(k in low for k in POLICE_SKIP) and tcat not in ("accident", "fire", "rescue"):
            continue
        if tcat not in ("accident", "fire", "rescue", "summary"):
            continue
        # A summary row must be a tally of incidents, not any headline that
        # happens to say 'weekend' — otherwise PR pieces leak in as summaries.
        if tcat == "summary" and not re.search(r"prometn|požar", low):
            continue
        published = f"{yy}-{mm}-{dd}"
        if published < cutoff:
            continue
        excerpt = tidy(strip_tags(excerpt_html))
        body = excerpt
        plain = fetch_article(base + href, _gov_article_text, budget_key=slug, budget=6)
        if plain:
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
            "title": cut_title(title),
            "location": place_label(title + " " + body) or place_case(seat),
            "lat": lat, "lon": lon, "units": ", ".join(units) or label,
            "crew": None, "vehicles": None, "raw": body[:1600], "link": base + href,
        })
    return out, status


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
        plain = fetch_article("https://policija.gov.hr" + href, lambda h: tidy(strip_tags(h)) or None,
                              budget_key="mup", budget=4)
        if not plain:
            continue
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
            "date": f"{y}-{int(m):02d}-{int(d):02d}", "time": "23:59", "category": "summary",
            "title": f"Prometne nesreče s ponesrečenimi — {total.group(1)} v RH"
                     + (f", {per[0]}" + (f"–{per[1]}" if per[1] != per[0] else "") if per else ""),
            "location": "Republika Hrvatska", "lat": None, "lon": None,
            "units": f"{killed if killed is not None else '?'} mrtvih · {injured if injured is not None else '?'} poškodovanih",
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
    # North-region additions. Krapina-Zagorje had zero coverage before this —
    # RHZK ("Radio Hrvatsko zagorje Krapina") looked promising but its
    # crna-kronika category is a dead archive (newest item from 2014);
    # zagorje.com's own /rss is live and current instead. Vzaktualno
    # complements the existing thin Varaždinski feed for Varaždin. ICV is
    # Virovitica-Podravina — not strictly "north" but a genuinely new county
    # found in the same search, confirmed live at multiple posts/day.
    ("Zagorje.com · kronika", "kzz", "https://www.zagorje.com/rss",
     "krapina", "zgc", False),
    ("Vzaktualno · kronika", "vaz", "https://vzaktualno.hr/category/crna-kronika/feed/",
     "varaždin", "vza", False),
    ("ICV · kronika", "vpz", "https://www.icv.hr/vijesti/crna-kronika/feed/",
     "virovitica", "icv", False),
    # Brigades' own per-call logs, found by scanning 8,567 candidate domains
    # (dvd-/jvp-/vatrogasci- × every Croatian city and municipality): 115 live
    # brigade sites, of which exactly these still post interventions in 2026.
    # The rest are statistics pages or logs that stopped in 2015-2025, and the
    # HVZ shared CMS (spis.hvz.hr) that hosts hundreds of DVDs is geo-fenced.
    ("DVD Supetar · intervencije", "sdz", "https://vatrogasci-supetar.hr/category/intervencije/feed/",
     "supetar", "sup", False),
    ("DVD Garčin · intervencije", "bpz", "https://dvdgarcin.hr/category/intervencije/feed/",
     "garčin", "gar", False),
    ("DVD Valpovo · intervencije", "obz", "https://dvd-valpovo.hr/category/intervencije/feed/",
     "valpovo", "val", False),
    # JVP Osijek: the category feed is disabled (404) but the query form works.
    ("JVP Osijek · intervencije", "obz", "https://vatrogasci-osijek.hr/?cat=3&feed=rss2",
     "osijek", "jvo", False),
    # Regional crna-kronika RSS filling two previously blind counties:
    # Dubrovnik-Neretva (only its police PU before) and Primorje-Gorski kotar /
    # Rijeka (two feeds — they miss different calls, both cheap). DuList
    # redirects to /crna-kronika/feed/. (Istra's only open feed, Regional
    # Express, is general news whose incidents this parser can't classify
    # reliably, so Istra keeps its police-PU coverage only.)
    ("DuList · kronika", "dnz", "https://dulist.hr/crna-kronika/feed/",
     "dubrovnik", "dul", False),
    ("Fiuman · kronika", "pgz", "https://www.fiuman.hr/category/crna-kronika/feed/",
     "rijeka", "fiu", False),
    ("Riportal · kronika", "pgz", "https://riportal.net.hr/kategorija/vijesti/crna-kronika/feed/",
     "rijeka", "rip", False),
    # National newsrooms (region=None → region from the place named; no place,
    # no row). The only afternoon coverage of Zagreb: the capital has no open
    # brigade log (JVP Zagreb's is credentialed) and its portals block bots.
    ("24sata · vijesti",   None, "https://www.24sata.hr/feeds/news.xml",     None, "24s", False),
    ("Index · vijesti",    None, "https://www.index.hr/rss/vijesti",         None, "idx", False),
    ("Jutarnji · vijesti", None, "https://www.jutarnji.hr/feed",             None, "jut", False),
    ("Večernji · vijesti", None, "https://www.vecernji.hr/feeds/latest",     None, "vec", False),
    ("tportal · vijesti",  None, "https://www.tportal.hr/rss-najnovije.xml", None, "tpo", False),
]


def region_for(lat: float, lon: float) -> str:
    """County code for a point: the nearest police-administration seat. Used for
    national newsrooms, whose items can be anywhere in the country."""
    best, bd = "nat", 9e9
    for _slug, region, _label, seat in POLICE_PUS:
        slat, slon = PLACES[seat]
        d = (slat - lat) ** 2 + ((slon - lon) * 0.7) ** 2
        if d < bd:
            best, bd = region, d
    return best


def make_newsroom(label, region, url, fallback_key, prefix, caps_lead):
    """Build a fetcher for one newsroom crna-kronika feed. Closes over the config
    so every regional newsroom shares one tested parser.

    region=None marks a NATIONAL feed (24sata, Index…): the item's region is
    then derived from the place it names, and an item that names no known
    place is dropped — a nationwide story with no location is not a call."""

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
            if blotter_skip(title, body):
                continue
            cat = categorise(body, title)
            if cat not in ("fire", "accident", "tech", "ems", "rescue"):
                continue
            # National feeds carry everything; there the headline itself must
            # name the incident, or a waste-audit story that mentions "sanacija"
            # in its body slips in as a call.
            if region is None and _cat(title.lower()) is None:
                continue
            # pubDate carries its own offset (+0000 on 24sata/Večernji, +0200 on
            # the WordPress portals) — parse it and convert to local wall clock.
            dt = _parse_pubdate(it.findtext("pubDate") or "")
            if dt is None:
                continue
            date, t = dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
            # An hour named inside the story beats the publication hour — but a
            # piece filed at 08:15 that names 21:40 is reporting last night.
            tm = (re.search(r"\b(?:oko|u)\s+(\d{1,2})[:.](\d{2})\s*(?:sati|h\b)", body)
                  or re.search(r"Vrijeme dojave:\s*(\d{1,2})[:.](\d{2})", body))
            if tm:
                date, t = event_when(date, t, f"{int(tm.group(1)):02d}:{tm.group(2)}")
            # Brigade logs posted in a batch ("Datum: 17.01.2026.") name the
            # call's own date; that beats the publication day outright.
            dm = re.search(r"\bDatum:\s*(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4})", body)
            if dm:
                cand = f"{dm.group(3)}-{int(dm.group(2)):02d}-{int(dm.group(1)):02d}"
                if cand <= date:
                    date = cand
            lead = re.match(r"^([A-ZŠĐČĆŽ][A-ZŠĐČĆŽ\s]{2,28}?)\s+[A-ZŠĐČĆŽ][a-zšđčćž]",
                            title) if caps_lead else None
            # The headline names where it happened; the body also names who
            # coordinated from where ("MRCC Rijeka" for a fire on Brač), so the
            # title is geocoded first and the body only when the title has no
            # known place.
            lead_txt = lead.group(1) + " " if lead else ""
            lat, lon = geocode(lead_txt + title)
            if lat is None:
                lat, lon = geocode(lead_txt + blob)
            if lat is None:
                if region is None:
                    continue                                   # national feed, no place named
                lat, lon = PLACES[fallback_key]
            out.append({
                "id": f"{prefix}:{tidy(it.findtext('guid') or it.findtext('link') or title)[-40:]}",
                "source": label, "region": region or region_for(lat, lon),
                "ref": f"{prefix.upper()}-{dt:%m%d-%H%M}", "date": date, "time": t,
                "category": cat, "title": cut_title(title),
                "location": (place_case(lead.group(1)) if lead and _match(lead.group(1)) else None)
                            or place_label(blob) or (place_case(fallback_key) if fallback_key else "Hrvatska"),
                "lat": lat, "lon": lon,
                "units": ", ".join(sorted({tidy(u) for u in UNIT_RE.findall(blob)})) or None,
                "crew": None, "vehicles": None, "raw": body[:1600],
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
            "category": categorise(title + " " + body), "title": cut_title(title),
            "location": place_label(title + " " + body) or "Horvati, Zagreb",
            "lat": lat, "lon": lon, "units": "DVD Horvati",
            "crew": None, "vehicles": None,
            "raw": body[:1600], "link": p.get("link", ""),
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
            pdt = _parse_pubdate(pub)                      # offset-aware, local wall clock
            date = (pdt or datetime.now(LOCAL)).strftime("%Y-%m-%d")
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
            "title": cut_title(title), "location": place_label(title + " " + body) or "Međimurje",
            "lat": lat, "lon": lon, "units": ", ".join(units) or "VZ Međimurske županije",
            "crew": int(tally.group(1)) if tally else None, "vehicles": None,
            "raw": body[:1600], "link": tidy(it.findtext("link") or ""),
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
    cutoff = (datetime.now(LOCAL) - timedelta(days=MAX_AGE_DAYS)).strftime("%Y-%m-%d")
    for url, title in arts[:12]:                       # newest first; cached after the first read
        if blotter_skip(title):
            continue
        plain = fetch_article(url, _gov_article_text, budget_key="sibenik.in", budget=6)
        if not plain:
            continue
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
        if blotter_skip(title, body):
            continue
        cat = categorise(body, title)
        if cat not in ("fire", "accident", "tech", "ems", "summary"):
            continue
        # The bulletin text usually names the call time; prefer it over the byline.
        tm = re.search(r"(?:dojav[ae]\s+(?:je\s+)?zaprimljena\s+u|u)\s+(\d{1,2})[:.](\d{2})\s*(?:h|sati)",
                       body)
        pub_t = f"{int(hh):02d}:{mi}"
        date, t = (event_when(date, pub_t, f"{int(tm.group(1)):02d}:{tm.group(2)}")
                   if tm else (date, pub_t))
        lat, lon = geocode(blob)
        if lat is None:
            lat, lon = PLACES["šibenik"]
        out.append({
            "id": f"sibin:{url.rstrip('/').rsplit('/', 1)[-1][:44]}",
            "source": "sibenik.in · kronika", "region": "sib",
            "ref": f"SI-{dd}{mm}-{hh}{mi}", "date": date, "time": t, "category": cat,
            "title": cut_title(title), "location": place_label(blob) or "Šibenik-Knin",
            "lat": lat, "lon": lon,
            "units": ", ".join(sorted({tidy(u) for u in UNIT_RE.findall(blob)})) or None,
            "crew": None, "vehicles": None, "raw": body[:1600], "link": url,
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
            "raw": body[:1600], "link": "https://jvp-sibenik.hr/",
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
        entries = [(tidy(x), None) for x in re.findall(r"^\s*-\s*(.+)$", body, re.M)]
        # Two kinds of bulletin share the header. The twelve-hour digest lists
        # "- " items; the ad-hoc "Priopćenje o požaru" flash (posted hourly while
        # a big fire runs: "na terenu se nalazi 35 vatrogasaca i 15 vozila,
        # sudjeluju i zračne snage") is one paragraph with no bullets at all —
        # and was being skipped wholesale. Treat that paragraph as the event,
        # timed at the bulletin's own stamp. The page renders each bulletin
        # twice (card + modal); the card copy reduces to "h OPŠIRNIJE" and is
        # dropped by the length check, the modal copy carries the text.
        if not entries:
            narr = tidy(re.sub(r"\bOPŠIRNIJE\b|^\s*h\b", " ", body))
            narr = re.sub(r"^(?:Priopćenje o [\wšđčćž]+\s*)+", "", narr).strip()
            if len(narr) >= 40:
                entries = [(narr, "flash")]
        for j, (item, kind) in enumerate(entries):
            if len(item) < 20:
                continue
            tm = re.search(r"(?:zaprimljena|dojava)[^\d]{0,20}(\d{1,2}:\d{2})", item)
            crew = re.search(r"(\d+)\s+vatrogas", item)
            veh = re.search(r"(\d+)\s+vozil", item)
            units = sorted({tidy(u) for u in UNIT_RE.findall(item)})
            lat, lon = geocode(item)
            # The bulletin's own stamp is the publication moment; an item hour
            # after it happened the evening before. Keep the id keyed to the
            # bulletin date so an already-stored row is corrected, not doubled.
            idate, it = (event_when(f"{yr}-{mon}-{day}", t, hhmm(tm.group(1)))
                         if tm else (f"{yr}-{mon}-{day}", t))
            # A flash bulletin describes the scene as it is: crews "na terenu" /
            # "u tijeku" means still running unless it says extinguished.
            fstatus = None
            if kind == "flash":
                fstatus = ("closed" if re.search(r"ugašen|završen", item, re.I)
                           else "contained" if re.search(r"lokaliziran|pod kontrolom", item, re.I)
                           else "active" if re.search(r"na terenu|u tijeku|traje|sudjeluj", item, re.I)
                           else None)
            out.append({
                "id": rowid("ŽVOC Šibenik-Knin", f"{yr}-{mon}-{day}", it, item),
                "source": "ŽVOC Šibenik-Knin", "region": "sib",
                "ref": f"ŽV-{day}{mon}-{'F' if kind == 'flash' else j+1}",
                "date": idate, "time": it, "status": fstatus,
                "category": categorise(item), "title": summarise(item),
                "location": place_label(item) or "Šibenik-Knin", "lat": lat, "lon": lon,
                "units": ", ".join(units), "crew": int(crew.group(1)) if crew else None,
                "vehicles": int(veh.group(1)) if veh else None,
                "raw": item[:1600],
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
        m = re.search(rf"/vijesti/(dvoc-[a-z0-9-]+)/{art_id}", listing)
        slug = m.group(1) if m else "dvoc-x"
        plain = fetch_article(f"https://hvz.gov.hr/vijesti/{slug}/{art_id}",
                              lambda h: tidy(strip_tags(h)) or None, budget_key="dvoc", budget=3)
        if plain is None and slug != "dvoc-x":
            plain = fetch_article(f"https://hvz.gov.hr/vijesti/dvoc-x/{art_id}",
                                  lambda h: tidy(strip_tags(h)) or None, budget_key="dvoc", budget=3)
        if not plain:
            continue
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
        rep_date = datetime.now(LOCAL).strftime("%Y-%m-%d")
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
            "title": f"Nočni pregled HVZ — {i2(nums.group(1))} intervencij",
            "location": per.group(1) if per else "Republika Hrvatska",
            "lat": None, "lon": None,
            "units": f"{i2(nums.group(2))} organizacij",
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


# CAP severity/certainty words, so the stored weather row reads Slovenian.
WX_SL = {"Minor": "manjša", "Moderate": "zmerna", "Severe": "huda", "Extreme": "izjemna",
         "Likely": "verjetno", "Possible": "možno", "Observed": "opaženo", "Unlikely": "malo verjetno"}


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
            exp = datetime.fromisoformat(expires.replace("Z", "+00:00")) if expires else None
            if exp is not None and exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp is not None and exp < now:
                continue                                  # drop lapsed warnings
        except (ValueError, TypeError):
            pass
        area = g("cap:areaDesc")
        lat, lon = geocode(area.replace(" region", ""))
        # CAP onset is an offset-carrying stamp (usually UTC); convert to local
        # wall clock before splitting, or a 13:00Z onset is stored as 13:00 local.
        onset = g("cap:onset")
        try:
            on = datetime.fromisoformat(onset.replace("Z", "+00:00"))
            on = on.astimezone(LOCAL) if on.tzinfo else on.replace(tzinfo=LOCAL)
            o_date, o_time = on.strftime("%Y-%m-%d"), on.strftime("%H:%M")
        except ValueError:
            o_date, o_time = onset[:10], onset[11:16]
        out.append({
            "id": f"meteo:{area}:{g('cap:event')}:{onset}",
            "source": "Meteoalarm", "region": "nat", "ref": f"WX-{i+1}",
            "date": o_date, "time": o_time,
            "category": "weather", "title": g("cap:event"),
            "location": area, "lat": lat, "lon": lon,
            "units": WX_SL.get(g("cap:severity"), g("cap:severity")), "crew": None, "vehicles": None,
            "raw": (f"{WX_SL.get(g('cap:severity'), g('cap:severity'))} · "
                    f"{WX_SL.get(g('cap:certainty'), g('cap:certainty'))} · velja do {expires}"),
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
        dt = _parse_pubdate(it.findtext("pubDate") or "")     # HGSS emits +0000
        if dt is None:
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
        loc = place_label(title + " " + body) or (place_case(station) if station else "Republika Hrvatska")
        out.append({
            "id": f"hgss:{tidy(it.findtext('guid') or it.findtext('link') or title)[-40:]}",
            "source": "HGSS · spašavanje", "region": "nat",
            "ref": f"HGSS-{dt:%m%d}", "date": dt.strftime("%Y-%m-%d"), "time": dt.strftime("%H:%M"),
            "category": "rescue", "title": cut_title(title), "location": loc,
            "lat": lat, "lon": lon,
            "units": f"HGSS {('Stanica ' + place_case(station)) if station else ''}".strip(),
            "crew": None, "vehicles": None, "raw": body[:1600],
            "link": tidy(it.findtext("link") or ""),
        })
    return out, status


def src_hac():
    """HAC — Hrvatske autoceste (the state motorway operator, distinct from the
    HAK auto club, whose own traffic map turned out to be a JS app with no
    discoverable public data endpoint short of executing it in a browser).

    A different kind of source entirely: not a newsroom or a brigade, but the
    infrastructure operator's own server-rendered "izvanredni događaj"
    (extraordinary event) log, one section per motorway stretch, covering the
    whole national network — including corridors (Slavonija, the A4 north)
    that no other source here reaches. Genuinely low frequency (roughly one
    event nationwide every couple of weeks) but zero overlap with anything
    else, and free: no JS, no auth, plain server-rendered HTML.
    """
    out = []
    text, status = fetch("https://www.hac.hr/hr/servisne-informacije/stanje-na-autocestama")
    if text is None:
        return out, status
    # Each motorway stretch is its own "<h4>name</h4> ... events ..." section;
    # split on the header so every event is attributed to the right stretch.
    parts = re.split(r'<h4 class="text-xl">([^<]+)</h4>', text)
    cutoff = (datetime.now(LOCAL) - timedelta(days=MAX_AGE_DAYS)).strftime("%Y-%m-%d")
    for i in range(1, len(parts) - 1, 2):
        stretch, section = tidy(parts[i]), parts[i + 1]
        for m in re.finditer(
                r'info-title">Izvanredni događaj</span>\s*<span class="font-medium">'
                r'(\d{2}):(\d{2})\s*(\d{2})\.(\d{2})\.(\d{4})</span>\s*<span\s*class="mt-1">'
                r'([^<]+)</span>', section):
            hh, mm, dd, mo, yy, desc = m.groups()
            date = f"{yy}-{mo}-{dd}"
            if date < cutoff:
                continue                                  # keep the archive out of the live parse
            desc = tidy(html_unescape(desc))
            # The event text often names an exit/town more precisely than the
            # stretch header; fall back to the first town named in the header.
            lat, lon = geocode(desc)
            if lat is None:
                lat, lon = geocode(stretch)
            out.append({
                "id": rowid("HAC", date, f"{hh}:{mm}", stretch + desc),
                "source": "HAC · autoceste", "region": "nat",
                "ref": f"HAC-{mo}{dd}-{hh}{mm}", "date": date, "time": f"{hh}:{mm}",
                "category": "tech", "title": cut_title(f"{stretch}: {desc}"),
                "location": place_label(desc) or stretch, "lat": lat, "lon": lon,
                "units": "HAC", "crew": None, "vehicles": None,
                "raw": desc[:1600],
                "link": "https://www.hac.hr/hr/servisne-informacije/stanje-na-autocestama",
            })
    return out, status


# ==========================================================================
# Austria — Oberösterreich (Upper Austria) and Niederösterreich (Lower
# Austria) state fire-brigade dispatch systems. A second country, run on a
# narrower rule than Croatia: live feed, pruned to AT_MAX_AGE_DAYS by an
# actual delete (see prune_supabase_austria), not just hidden client-side.
#
# Both are real state fire-command dispatch logs, server-rendered, no auth,
# no JS — genuinely live, minute-to-second precision, verified by hand
# against the running services. Training exercises ARE kept (category
# "exercise") so the console can show them, distinctly marked, rather than
# silently dropping them; and each call carries a real status: NÖ from its
# separate "currently running" endpoint, OÖ from per-unit end times.
# ==========================================================================
AT_MAX_AGE_DAYS = 3
# Styria/Carinthia after-action reports are posted hours to days after the
# call and a district publishes a few a week; a 3-day window would leave the
# border belt empty most of the time, so those rows live a week.
AT_REPORT_MAX_AGE_DAYS = 7

# German → Slovenian glossary for the fixed vocabulary these systems use.
# A phrase glossary, not a general translator: longest phrases match first,
# anything unknown stays German rather than being guessed at, and the
# untouched German original always survives in `raw`. Every string below
# was observed in the live feeds (NÖ: 30 type strings; OÖ: ~45), plus the
# obvious siblings of each.
AT_SL_GLOSSARY = [
    # ── NÖ fixed type-code phrases ──
    ("Brandsicherheitswache", "požarna straža (dežurstvo)"),
    ("Fahrzeugbrand - Klein", "požar vozila – manjši"),
    ("Fahrzeugbrand - PKW", "požar osebnega vozila"),
    ("Fahrzeugbrand - LKW", "požar tovornega vozila"),
    ("Fahrzeugbrand", "požar vozila"),
    ("Gefahrenmeldeanlage - Brand", "sprožen požarni javljalnik"),
    ("Gefahrenmeldeanlage", "javljalnik nevarnosti"),
    ("Kleinbrand - im Freien", "manjši požar na prostem"),
    ("Kleinbrand", "manjši požar"),
    ("Rauchentwicklung", "razvoj dima (dimljenje)"),
    ("Vegetationsbrand - Freifläche", "požar vegetacije na odprtem"),
    ("Vegetationsbrand", "požar vegetacije"),
    ("Gebäudebrand - Landwirtschaft", "požar kmetijske zgradbe"),
    ("Gebäudebrand - Wohnhaus", "požar stanovanjske hiše"),
    ("Gebäudebrand", "požar zgradbe"),
    ("Austritt - Betriebsmittel", "iztekanje pogonskih tekočin"),
    ("Erkundung/Kontrolle", "izvidovanje/pregled"),
    ("Anforderung - von anderer Organisation", "zahteva druge organizacije (pomoč)"),
    ("Arbeitseinsatz / Technische Hilfeleistung", "delovna akcija / tehnična pomoč"),
    ("Technische Hilfeleistung", "tehnična pomoč"),
    ("Insekteneinsatz", "odstranitev žuželk (sršeni/ose)"),
    ("Logistikeinsatz", "logistična podpora"),
    ("Wasserversorgung", "oskrba z vodo"),
    ("Auspumparbeiten", "izčrpavanje vode"),
    ("Bergung - Arbeitsmaschine", "izvlek delovnega stroja"),
    ("Bergung - Großfahrzeug", "izvlek velikega vozila"),
    ("Bergung - Kleinfahrzeug", "izvlek manjšega vozila"),
    ("Bergung - PKW", "izvlek osebnega vozila"),
    ("Bergung - LKW", "izvlek tovornega vozila"),
    ("Bergung PKW", "izvlek osebnega vozila"),
    ("Bergung", "izvlek/reševanje"),
    ("Notöffnung - Lift", "nujno odpiranje dvigala"),
    ("Notöffnung - Tür", "nujno odpiranje vrat"),
    ("Notöffnung", "nujno odpiranje"),
    ("Objekt/Baum - Umgestürzt", "podrto drevo/objekt"),
    ("Tierrettung", "reševanje živali"),
    ("Verkehrsunfall - Verletzungen", "prometna nesreča s poškodovanimi"),
    ("Wassergebrechen", "okvara vodovoda / izliv vode"),
    ("Menschenrettung - Höhe/Tiefe", "reševanje osebe z višine/globine"),
    ("Menschenrettung - Notlage", "reševanje osebe v stiski"),
    ("Menschenrettung", "reševanje osebe"),
    ("Übung", "vaja"),
    # ── OÖ free-text phrases ──
    ("Einsatz od. Einsatzübung", "intervencija ali vaja (nerazvrščen alarm)"),
    ("Einsatzübung", "vaja"),
    ("Aufzugsdefekt", "okvara dvigala"),
    ("Baum droht umzustürzen", "drevo grozi, da se podre"),
    ("Brand Bahndamm", "požar železniškega nasipa"),
    ("Brand Baumaschine im Freien", "požar gradbenega stroja na prostem"),
    ("Brand Elektroanlage in Gebäude", "požar električne napeljave v zgradbi"),
    ("Brand Fahrzeug in Gebäude", "požar vozila v zgradbi"),
    ("Brand Fassade", "požar fasade"),
    ("Brand Feld", "požar polja"),
    ("Brand Fluren", "požar travnikov/njiv"),
    ("Brand Gebäude mehrstöckig", "požar večnadstropne zgradbe"),
    ("Brand Gebäude", "požar zgradbe"),
    ("Brand Gebüsch", "požar grmovja"),
    ("Brand Gewerbe", "požar obrtnega/industrijskega objekta"),
    ("Brand Kamin", "požar dimnika"),
    ("Brand Kübel im Freien", "požar smetnjaka na prostem"),
    ("Brand PKW im Freien", "požar osebnega vozila na prostem"),
    ("Brand Schuppen", "požar lope"),
    ("Brand Wiese", "požar travnika"),
    ("Brand im Dachbereich", "požar v ostrešju"),
    ("Brand im Freien", "požar na prostem"),
    ("Brand landwirtschaftliches Fahrzeug", "požar kmetijskega vozila"),
    ("Brand landwirtschaftliches Objekt", "požar kmetijskega objekta"),
    ("Brand unklare Lage", "požar – nejasno stanje"),
    ("Brandmeldealarm", "alarm požarnega javljalnika"),
    ("Brandmeldetaste Gedrückt", "pritisnjen ročni javljalnik požara"),
    ("Brandverdacht", "sum požara"),
    ("Brandgeruch", "vonj po zažganem"),
    ("Brandnachschau", "naknadni pregled požarišča"),
    ("Eingeschlossene Person in Lift", "oseba ujeta v dvigalu"),
    ("Entlaufenes Tier", "pobegla žival"),
    ("Freimachen von Verkehrswegen", "sprostitev prometnih poti"),
    ("Gasgeruch wahrnehmbar", "zaznan vonj po plinu"),
    ("Gasaustritt", "uhajanje plina"),
    ("Gebäude droht überflutet zu werden", "zgradbi grozi poplava"),
    ("Keller überflutet", "poplavljena klet"),
    ("Kohlenmonoxidaustritt", "uhajanje ogljikovega monoksida (CO)"),
    ("Person eingeklemmt", "ukleščena oseba"),
    ("Person in misslicher Lage", "oseba v nevarnem položaju"),
    ("Personenrettung Verkehrsunfall LKW", "reševanje oseb – prometna nesreča s tovornjakom"),
    ("Personenrettung Verkehrsunfall PKW", "reševanje oseb – prometna nesreča z osebnim vozilom"),
    ("Personenrettung hoch", "reševanje osebe z višine"),
    ("Personenrettung", "reševanje oseb"),
    ("Personensuche", "iskanje pogrešane osebe"),
    ("Verkehrsunfall mit eingeklemmter Person", "prometna nesreča z ukleščeno osebo"),
    ("Kaminbrand", "požar dimnika"), ("Flurbrand", "požar travnika/polja"),
    ("Wohnungsbrand", "požar stanovanja"), ("Zimmerbrand", "požar sobe"),
    ("Küchenbrand", "požar v kuhinji"), ("Waldbrand", "gozdni požar"),
    ("Müllbrand", "požar odpadkov"), ("Containerbrand", "požar zabojnika"),
    ("Evakuierung - Sofort", "evakuacija – takoj"), ("Evakuierung", "evakuacija"),
    ("Rettung Kleintier", "reševanje male živali"),
    ("Sonstiger Einsatz", "druga intervencija"),
    ("Tragehilfe", "pomoč pri prenosu pacienta (asistenca NMP)"),
    ("Türöffnung Menschenrettung", "odpiranje vrat – reševanje osebe"),
    ("Türöffnung Unfallverdacht", "odpiranje vrat – sum nesreče"),
    ("Türöffnung", "odpiranje vrat"),
    ("Verkehrsunfall Aufräumarbeiten", "čiščenje po prometni nesreči"),
    ("Verkehrsunfall", "prometna nesreča"),
    ("Wasserschaden", "škoda zaradi vode"),
    ("Sturmschaden", "škoda zaradi neurja"),
    ("ÖWR Einsatz - Defektes Boot auf Gewässer", "vodna reševalna služba – okvarjen čoln"),
    ("ÖWR Einsatz", "intervencija vodne reševalne službe"),
    ("Ölaustritt groß", "večje iztekanje olja"),
    ("Ölspur/Ölaustritt", "sled olja / iztekanje olja"),
    ("Ölaustritt", "iztekanje olja"),
    ("Ölspur", "sled olja na cesti"),
    # ── EMS / helicopter phrasing, when it appears in fire-brigade logs ──
    ("Hubschrauberlandeplatz", "varovanje pristajališča helikopterja"),
    ("Hubschrauberlandung", "pristanek reševalnega helikopterja"),
    ("Notarzthubschrauber", "reševalni helikopter (NMP)"),
    ("Hubschrauber", "helikopter"),
    ("Rettungsdienst", "reševalna služba"),
    ("Notarzt", "zdravnik NMP"),
    # ── generic connective words, applied last ──
    ("im Freien", "na prostem"),
    ("Brand", "požar"),
    ("Unfall", "nesreča"),
    ("Person", "oseba"),
]


def at_translate(de_text: str) -> str:
    """Best-effort German→Slovenian gloss over the fixed dispatch vocabulary.
    Unmatched words stay German rather than being mistranslated; `raw`
    always keeps the untouched German original."""
    out = de_text
    for de, sl in AT_SL_GLOSSARY:
        # Long phrases may match inside compounds ("Gasaustritt" is listed as
        # such); the short generic words must not — "Person" inside
        # "Personensuche" once produced "osebaensuche". Word-bound the short ones.
        pat = re.escape(de) if len(de) > 7 else r"\b" + re.escape(de) + r"\b"
        out = re.sub(pat, sl, out)
    return out


# Category from the German type text. Order matters: the more specific
# emergency kinds first, so "Personenrettung Verkehrsunfall" lands in
# accident and "Türöffnung Menschenrettung" in rescue.
AT_CATCODE = [
    (r"^U\d|Übung|Einsatzübung", "exercise"),
    (r"Hubschrauber|Landeplatz|Notarzt|Tragehilfe|Rettungsdienst", "ems"),
    (r"Verkehrsunfall", "accident"),
    (r"Menschenrettung|Personenrettung|Person eingeklemmt|Person in misslicher|"
     r"Eingeschlossene Person|Tierrettung|Rettung Kleintier|Entlaufenes Tier", "rescue"),
    (r"^B\d|Brand|Rauch", "fire"),
    (r"^S\d|Austritt|Ölspur|Gasgeruch|Kohlenmonoxid", "tech"),
    (r"^T\d|Bergung|Notöffnung|Türöffnung|Wasser|Sturm|Baum|Aufzug|Lift|Auspump|"
     r"überflutet|Insekten|Logistik|Arbeitseinsatz|Freimachen|Technische", "tech"),
    (r"^SOF|Erkundung|Anforderung|Sonstiger", "other"),
]


def _at_category(type_text: str) -> str:
    # Case-insensitive: German compounds bury the keyword mid-word
    # ("Gasaustritt", "Kohlenmonoxidaustritt"), where a capitalised
    # "Austritt" would never match.
    for pat, cat in AT_CATCODE:
        if re.search(pat, type_text, re.I):
            return cat
    return "other"


def _at_geocode(town: str, state: str):
    """Reuse the Croatian-street-level Nominatim pipeline for Austrian towns —
    same rate limit, same persistent geocode_cache table (so a recurring town
    is never re-geocoded across polls — NÖ alone repeats the same handful of
    towns dozens of times a day), just a different query string and country
    filter. Never geocoded against the Croatian PLACES gazetteer, which is
    HR-only."""
    conn = cache_db()
    town = tidy(town or "")
    if not town:
        return (None, None)
    key = f"AT|{town.lower()}|{state}"                # case-normalised: 'Bad schönau' is Bad Schönau
    found, lat, lon = _geo_cached(conn, key)
    if found:
        return (lat, lon)
    hit = _nominatim_lookup(f"{town}, {state}, Österreich", countrycodes="at")
    if hit is None:
        # Second try without the state: a town on a state border, or one whose
        # district name the page uses, often resolves country-wide.
        hit = _nominatim_lookup(f"{town}, Österreich", countrycodes="at")
    if hit in ("error", "budget"):
        return (None, None)                           # transient: not cached, retried next cycle
    lat, lon = hit if hit else (None, None)
    # A hit is cached for good; a miss for a day (negative cache), so a typo
    # town is not queried on every poll.
    _geo_remember(conn, key, lat, lon)
    return (lat, lon)


def fetch_post(url: str, data: dict, timeout: int = 30):
    """Plain POST (no conditional-GET machinery). OÖ's exercise toggle is a
    POST form, and the exercise rows are exactly the ones the console is
    asked to show, so the poll has to submit it."""
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8", "Accept-Language": "de,en;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            charset = resp.headers.get_content_charset()
            for enc in filter(None, (charset, "utf-8", "cp1252")):
                try:
                    return raw.decode(enc), "ok"
                except (UnicodeDecodeError, LookupError):
                    continue
            return raw.decode("utf-8", "replace"), "ok"
    except urllib.error.HTTPError as e:
        return None, f"error: HTTP {e.code}"
    except Exception as e:                                        # noqa: BLE001
        return None, f"error: {type(e).__name__}"


_WASTL = "https://www.feuerwehr-krems.at/CodePages/Wastl/WastlMain/"
_WASTL_ROW = (r">Alarmzentrale</td><td[^>]*>([^<]*)</td><td[^>]*>([^<]*)</td>"
              r"<td[^>]*>(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2}):(\d{2})</td>")


def src_at_noe():
    """Niederösterreich (Lower Austria) — the "Wastl" statewide fire dispatch
    system (feuerwehr-krems.at), reached through several nested pages found
    by hand (BFKDO Wiener Neustadt → iframe → Wastl overview → iframe →
    Land_EinsatzHistorie.asp). Real dispatch log, precise to the second.

    Two lists, no overlap: the history (Land_EinsatzHistorie.asp) holds
    closed calls with an exact second-precision timestamp; the "currently
    running" page (Land_EinsatzAktuell.asp) holds the open ones with only a
    date and a rough duration ("~ 1 std."). A running call is emitted as its
    own row (time approximated from the duration, ref NOE-LIVE-…); it
    self-expires once it leaves the running list (prune_supabase_austria
    drops LIVE rows not re-seen within 40 minutes), and the exact-time
    history row takes over. Training exercises (U-prefix) are kept and
    tagged "exercise".
    """
    out = []
    text, status = fetch(_WASTL + "Land_EinsatzHistorie.asp?bezirk=&vc")
    if text is None:
        return out, status
    now = datetime.now(LOCAL)
    cutoff = (now - timedelta(days=AT_MAX_AGE_DAYS)).strftime("%Y-%m-%d")
    for town, typ, dd, mo, yy, hh, mi, ss in re.findall(_WASTL_ROW, text):
        town, typ = tidy(town), tidy(typ)
        date = f"{yy}-{mo}-{dd}"
        if date < cutoff:
            continue
        lat, lon = _at_geocode(town, "Niederösterreich")
        out.append({
            "id": rowid("AT-NOE", date, f"{hh}:{mi}", town + typ),
            "source": "NÖ Feuerwehr · Wastl", "region": "noe", "country": "AT",
            "ref": f"NOE-{mo}{dd}-{hh}{mi}{ss}", "date": date, "time": f"{hh}:{mi}",
            "category": _at_category(typ), "title": at_translate(typ), "status": "closed",
            "location": town, "lat": lat, "lon": lon,
            # The page names the alarm place, not the responding brigade — no
            # invented "Feuerwehr <town>"; the console handles an empty unit.
            "units": None, "crew": None, "vehicles": None,
            "raw": typ, "link": _WASTL + "ShowOverview.asp",
        })
    live, _ = fetch(_WASTL + "Land_EinsatzAktuell.asp?vc")
    conn = cache_db()
    for town, typ, when in re.findall(
            r">Alarmzentrale</td><td[^>]*>([^<]*)</td><td[^>]*>([^<]*)</td><td[^>]*>([^<]*)</td>",
            live or ""):
        town, typ = tidy(town), tidy(typ)
        m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})\s*~\s*(\d+)\s*(std|min)", when, re.I)
        if not m:
            continue
        # A fire watch at an event (B0 Brandsicherheitswache) runs for days and
        # is a standby duty, not a running call — keep it off the live list.
        if re.search(r"Brandsicherheitswache|^SOF0", typ, re.I):
            continue
        dd, mo, yy, n, unit = m.groups()
        page_date = f"{yy}-{mo}-{dd}"
        rid = rowid("AT-NOE-LIVE", page_date, "", town + typ)
        # The page gives only "~ N std." — an estimate that drifts a minute per
        # poll if recomputed. Persist the first estimate (rounded to the page's
        # own precision) and keep it for the life of the call, and date it from
        # the estimate too so a call that started before midnight is not stamped
        # with today's date and tomorrow's-looking hour.
        known = conn.execute("SELECT started FROM live_start WHERE id=?", (rid,)).fetchone()
        if known is not None:
            started = datetime.fromisoformat(known["started"])
        else:
            hours = unit.lower().startswith("std")
            started = now - (timedelta(hours=int(n)) if hours else timedelta(minutes=int(n)))
            started = (started.replace(minute=0, second=0, microsecond=0) if hours
                       else started.replace(second=0, microsecond=0))
            with conn:
                conn.execute("INSERT OR REPLACE INTO live_start(id, started, seen_at) VALUES(?,?,?)",
                             (rid, started.isoformat(timespec="minutes"),
                              datetime.now(timezone.utc).isoformat(timespec="seconds")))
        lat, lon = _at_geocode(town, "Niederösterreich")
        out.append({
            "id": rid,
            "source": "NÖ Feuerwehr · Wastl", "region": "noe", "country": "AT",
            "ref": f"NOE-LIVE-{mo}{dd}", "date": started.strftime("%Y-%m-%d"),
            "time": started.strftime("%H:%M"),
            "category": _at_category(typ), "title": at_translate(typ), "status": "active",
            "location": town, "lat": lat, "lon": lon,
            "units": None, "crew": None, "vehicles": None,
            "raw": f"{typ} (v teku ~{n} {'h' if unit.lower().startswith('std') else 'min'})",
            "link": _WASTL + "ShowOverview.asp",
        })
    return out, status


_OOE_ROW = re.compile(
    r'<td style="background-color: (\w+)">&nbsp;</td><td[^>]*><b>([^<]+)</b>\s*'
    r'\(<a title="([^"]+)">(\w+)</a>\):\s*([^<]+)<br>\s*<small><ul>(.*?)</ul></small>', re.S)
_OOE_UNIT = re.compile(
    r'<li>([^:<]+):&nbsp;(\d{2})\.(\d{2})\.\s*(\d{2}):(\d{2})(?:&nbsp;&ndash;&nbsp;(\d{2}):(\d{2}))?')


def src_at_ooe():
    """Oberösterreich (Upper Austria) — the state fire-brigade association's
    own live operations page (einsaetze.ooelfv.at). The 2-day view, with the
    exercise toggle submitted (a POST form), which is what makes the "Einsatz
    od. Einsatzübung" rows appear — the unconfirmed alarm-or-exercise calls
    the page hides by default. Each responding brigade carries its own alert
    time and, once released, an end time: a call is closed when every unit
    has one, otherwise still active. Earliest alert time is the call's time.
    """
    out = []
    text, status = fetch_post("https://einsaetze.ooelfv.at/einsatz/2tage",
                              {"exercise": "Y", "bezirk": "-1"})
    if text is None:
        return out, status
    now = datetime.now(LOCAL)
    cutoff = (now - timedelta(days=AT_MAX_AGE_DAYS)).strftime("%Y-%m-%d")
    for colour, town, district, _abbr, typ, unit_block in _OOE_ROW.findall(text):
        town, typ = tidy(town), tidy(typ)
        units = _OOE_UNIT.findall(unit_block)
        if not units:
            continue
        first = min(units, key=lambda u: (u[2], u[1], u[3], u[4]))       # earliest alert
        _u, dd, mo, hh, mi, _eh, _em = first
        year = now.year - (1 if int(mo) > now.month else 0)               # year-end wrap
        date = f"{year}-{mo}-{dd}"
        if date < cutoff:
            continue
        closed = all(u[5] for u in units)
        lat, lon = _at_geocode(town, "Oberösterreich")
        cat = _at_category(typ)
        if colour == "grey" and cat == "other":
            cat = "exercise"
        out.append({
            "id": rowid("AT-OOE", date, f"{hh}:{mi}", town + typ),
            "source": "OÖ Feuerwehr · LFV", "region": "ooe", "country": "AT",
            "ref": f"OOE-{mo}{dd}-{hh}{mi}", "date": date, "time": f"{hh}:{mi}",
            "category": cat, "title": at_translate(typ),
            "status": "closed" if closed else "active",
            "location": f"{town} ({district})", "lat": lat, "lon": lon,
            "units": ", ".join(tidy(u[0]) for u in units), "crew": None, "vehicles": len(units),
            "raw": typ, "link": "https://einsaetze.ooelfv.at/einsatz/2tage",
        })
    return out, status


# ── Border belt with Slovenia: Styria + Carinthia ─────────────────────────
# Neither state exposes a reachable dispatch log: Styria's live overview
# (einsatzuebersicht.lfv.steiermark.at) does not answer from outside Austria,
# Carinthia's (feuerwehr.einsatz.or.at) is login-only. What is open are the
# after-action reports the district commands and the border brigades publish
# on their WordPress/Jimdo sites — per call, dated, with the alarm time in the
# text — plus the Styrian LFV's statewide daily report list. Slower than a
# dispatch feed (hours, not minutes) but real per-incident data for the belt
# from Bad Radkersburg to Villach.
#   (label, region, state, feed URL, ref prefix, fallback (lat, lon) when the
#    text names no town — the district's seat)
AT_FEEDS = [
    ("BFKDO Klagenfurt-Land · poročila", "ktn", "Kärnten",
     "https://www.bfkdo-klagenfurtland.at/category/einsaetze/feed/", "KTN-KL", (46.62, 14.30)),
    ("BFKDO Villach-Land · poročila", "ktn", "Kärnten",
     "https://www.bfkdo-villachland.at/category/einsaetze/feed/", "KTN-VL", (46.60, 13.85)),
    ("BFKDO Wolfsberg · poročila", "ktn", "Kärnten",
     "https://www.bfkdo-wolfsberg.at/category/berichte/einsaetze/feed/", "KTN-WO", (46.84, 14.84)),
    ("BFK Völkermarkt · poročila", "ktn", "Kärnten",
     "https://www.bfk-voelkermarkt.at/wp/feed/", "KTN-VK", (46.66, 14.63)),
    ("FF Völkermarkt · poročila", "ktn", "Kärnten",
     "https://www.ffvk.at/category/einsatzberichte/feed/", "KTN-FFVK", (46.662, 14.634)),
    ("FF Lavamünd · poročila", "ktn", "Kärnten",
     "https://www.feuerwehr-lavamuend.at/category/einsatz/feed/", "KTN-LAV", (46.64, 14.95)),
    ("FF Mureck · poročila", "stmk", "Steiermark",
     "https://www.feuerwehr-mureck.at/category/einsatzberichte/feed/", "STMK-MUR", (46.71, 15.77)),
    ("FF Bad Radkersburg · poročila", "stmk", "Steiermark",
     "https://www.ff-badradkersburg.at/rss/blog", "STMK-RAD", (46.685, 15.985)),
    ("FF Feldbach · poročila", "stmk", "Steiermark",
     "https://www.feuerwehr-feldbach.at/feed/", "STMK-FB", (46.95, 15.89)),
    # Two brigades right on the Slovenian border in Bezirk Leibnitz — their
    # site-wide WordPress feeds, like Mureck/Radkersburg above.
    ("FF Vogau · poročila", "stmk", "Steiermark",
     "https://www.ffvogau.at/feed/", "STMK-VOG", (46.735, 15.585)),
    ("FF Spielfeld · poročila", "stmk", "Steiermark",
     "https://www.ff-spielfeld.at/feed/", "STMK-SPF", (46.708, 15.636)),
]

# Posts on these sites that are not interventions: competitions, anniversaries,
# blessings, elections, courses. An exercise ("Übung") is kept and tagged.
AT_REPORT_SKIP = re.compile(
    r"Ausflug|Jubil|\d+\s*Jahre|Leistungsprüf|Leistungsabzeichen|Bewerb|fest\b|Frühschoppen|Ehrung|"
    r"Angelobung|Wahl\b|Kurs\b|Lehrgang|Spende|Weihnacht|Nikolaus|Ferien|Tag der offenen|"
    r"versammlung|Wissenstest|Segnung|Rückblick|Statistik|vergangenen Wochen|Jahresbericht|"
    r"Neues Fahrzeug|Fahrzeugübergabe|Sanitäter|Ausbildung|Florian|Dank\b", re.I)
# Narrative German, not type codes: what the report is about.
AT_REPORT_CATCODE = [
    (r"Übung", "exercise"),
    (r"Hubschrauber|Notarzt|Rettungsdienst|Tragehilfe|Rettungshubschrauber", "ems"),
    (r"Unfall|VU\s?\d|prallt|Kollision|Zusammensto|Motorrad|überschlag|von der Fahrbahn|streift|"
     r"Zwischenfall|Entgleis|\bZug\b|Frontal", "accident"),
    (r"Brand|brennt|Feuer\b|Rauch|Flammen", "fire"),
    (r"Suchaktion|vermisst|Person|Menschenrettung|Personenrettung|Tier|Katze|Hund|Kuh|Pferd", "rescue"),
    (r"Unwetter|Sturm|Hochwasser|Überflut|Starkregen|Schnee|Baum|Ölspur|Öl\b|Gas|Chlor|Schadstoff|"
     r"geborgen|Bergung|Wasserschaden|Auspump|Keller|Fahrzeugbergung|Türöffnung|Technisch", "tech"),
    (r"Einsatz|Alarm|alarmiert", "other"),
]
_AT_TOWN_STOP = {"Bronze", "Silber", "Gold", "Brand", "Vollbrand", "Flammen", "Kürze", "Einsatz",
                 "Zusammenarbeit", "Not", "Gefahr", "Aktion", "Sicherheit", "Höhe", "Tiefe", "Richtung",
                 "Folge", "Zukunft", "Bewegung", "Kärnten", "Steiermark", "Österreich", "Bereich",
                 "Abschnitt", "Bezirk", "Land", "Wald", "Wiese", "Feld", "Garage", "Keller", "Haus",
                 "Wohnhaus", "Gebäude", "Schwierigkeiten", "Vollalarm", "Dauereinsatz", "Sommer",
                 "Frühjahr", "Herbst", "Winter", "Nacht", "Fahrt", "Anwesenheit", "Betrieb", "See",
                 # police-release phrasing
                 "Fahrtrichtung", "Unfall", "Straßenkilometer", "Zuge", "Nähe", "Ortsgebiet",
                 "Gemeindegebiet", "Freiland", "Kreuzungsbereich", "Krankenhaus", "Landesklinikum",
                 "Klinikum", "Spital", "Ambulanz", "Anschluss", "Zusammenhang", "Begleitung",
                 "Abstimmung", "Verdacht", "Untersuchungshaft", "Anwesen", "Umgebung", "Ortschaft",
                 "Verlauf", "Ersttäter", "Sturzflug", "Am", "Im", "An", "Bezirk", "Ort", "Hand"}
_AT_TOWN_RE = re.compile(
    r"\((?:Markt|Stadt)?[Gg]emeinde\s+([A-ZÄÖÜ][\wäöüß.-]+(?:\s+(?:am|an der|im|ob|bei|in der)\s+[A-ZÄÖÜ][\wäöüß-]+)?)\)"
    r"|\b(?:in|bei)\s+([A-ZÄÖÜ][\wäöüß-]{2,}(?:\s+(?:am|an der|im|ob|bei)\s+[A-ZÄÖÜ][\wäöüß-]+)?)")


def _at_report_category(text: str):
    if re.search(r"Übung", text):
        return "exercise"
    if AT_REPORT_SKIP.search(text):
        return None
    for pat, cat in AT_REPORT_CATCODE:
        if re.search(pat, text, re.I):
            return cat
    return None


def _at_report_town(text: str):
    """First place name the report names; None when it only names a road."""
    for m in _AT_TOWN_RE.finditer(text):
        town = (m.group(1) or m.group(2)).strip(".-")
        if town.split()[0] not in _AT_TOWN_STOP and not re.match(r"[A-Z]\d", town):
            return town
    return None


def _at_report_when(text: str, published: datetime):
    """Alarm date/time from the narrative ("Am 06.09.2026 um 09:29 Uhr …",
    "gegen 16:10 Uhr"), else the publication stamp. A narrative date in the
    future (a typo) falls back to the publication date."""
    date = published.strftime("%Y-%m-%d")
    m = re.search(r"\b[Aa]m\s+(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4})", text)
    cand = None
    if m:
        cand = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    else:
        # Police releases spell the month out: "Montagabend, 7. September 2026".
        m = re.search(r"\b(\d{1,2})\.\s*(J[äa]nner|Januar|Februar|März|April|Mai|Juni|Juli|August|"
                      r"September|Oktober|November|Dezember)\s+(\d{4})", text)
        if m:
            mon = ["jänner", "februar", "märz", "april", "mai", "juni", "juli", "august",
                   "september", "oktober", "november", "dezember"]
            name = m.group(2).lower().replace("januar", "jänner").replace("janner", "jänner")
            cand = f"{m.group(3)}-{mon.index(name) + 1:02d}-{int(m.group(1)):02d}"
    if cand and cand <= published.strftime("%Y-%m-%d"):
        date = cand
    t = re.search(r"(?:um|gegen)\s+(\d{1,2})[:.](\d{2})\s*Uhr", text, re.I)
    time_ = f"{int(t.group(1)):02d}:{t.group(2)}" if t else published.strftime("%H:%M")
    return date, time_


def _parse_pubdate(pub: str):
    """RFC 822 pubDate → aware datetime in LOCAL. The offset in the feed is
    honoured ('+0000' on 24sata/Večernji/HGSS, '+0200' on WordPress portals);
    'GMT'/'UTC' names count as UTC; a stamp with no zone at all is taken as
    local wall clock."""
    pub = tidy(pub or "")
    if not pub:
        return None
    if not re.match(r"^[A-Za-z]{3},", pub):
        pub = "Mon, " + pub                                    # weekday missing: tolerate
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S",
                "%a, %d %b %Y %H:%M %z", "%a, %d %b %Y %H:%M"):
        try:
            dt = datetime.strptime(pub, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc if pub.upper().endswith(("GMT", "UTC", " Z")) else LOCAL)
        return dt.astimezone(LOCAL)
    return None


def make_at_feed(label, region, state, url, prefix, fallback):
    """One fetcher for every border-belt report feed (WordPress or Jimdo RSS)."""

    def fetch_source():
        out = []
        text, status = fetch(url)
        if text is None:
            return out, status
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return out, "error: bad XML"
        cutoff = (datetime.now(LOCAL) - timedelta(days=AT_REPORT_MAX_AGE_DAYS)).strftime("%Y-%m-%d")
        ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
        for it in root.findall(".//item"):
            title = tidy(strip_tags(it.findtext("title") or ""))
            body = tidy(strip_tags((it.findtext("content:encoded", namespaces=ns)
                                    or it.findtext("description") or "")))
            body = re.sub(r"Der Beitrag .*? erschien zuerst auf .*?\.", "", body)
            published = _parse_pubdate(it.findtext("pubDate") or "")
            if published is None:
                continue
            # A headline that is itself a competition/anniversary/course is final —
            # its body ("… Brand …") must not smuggle it back in as a fire.
            if AT_REPORT_SKIP.search(title) and not re.search(r"Übung", title):
                continue
            cat = _at_report_category(title) or _at_report_category(body[:300])
            if cat is None:
                continue
            date, time_ = _at_report_when(body, published)
            if date < cutoff:
                continue
            town = _at_report_town(title) or _at_report_town(body[:400])
            lat, lon = _at_geocode(town, state) if town else (None, None)
            if lat is None:
                lat, lon = fallback
            units = sorted({tidy(u) for u in re.findall(r"\b(?:FF|BF|Feuerwehr)\s+[A-ZÄÖÜ][\wäöüß-]+(?:\s+(?:am|ob|im|an der)\s+[A-ZÄÖÜ][\wäöüß-]+)?", body)})
            out.append({
                "id": rowid("AT-" + prefix, date, time_, title),
                "source": label, "region": region, "country": "AT",
                "ref": f"{prefix}-{date[5:7]}{date[8:]}-{time_.replace(':', '')}",
                "date": date, "time": time_, "category": cat, "title": title[:120],
                "status": "closed",
                "location": (town or label.split(" ·")[0].replace("BFKDO ", "").replace("BFK ", "").replace("FF ", "")) + f" ({state})",
                "lat": lat, "lon": lon,
                "units": ", ".join(units) or None, "crew": None, "vehicles": len(units) or None,
                "raw": body[:1600], "link": tidy(it.findtext("link") or ""),
            })
        return out, status

    return fetch_source


# ── Austrian police: the Interior Ministry's per-state press feeds ─────────
# Official, per-incident, several a day, with the incident's date, hour and
# place in the text — the Austrian counterpart of the Croatian PU pages. The
# feeds carry everything the police say, so pure crime (fraud, burglary,
# arrests, investigations) is filtered out, the same rule as for Croatia: this
# is an emergency console, not a police blotter.
#   (state code in the feed URL, label, region, state name, fallback (lat, lon))
AT_POLICE = [
    ("stmk", "Policija Štajerska · LPD", "stmk", "Steiermark", (47.20, 15.30)),
    ("ktn",  "Policija Koroška · LPD",   "ktn",  "Kärnten",    (46.70, 14.10)),
    ("noe",  "Policija Sp. Avstrija · LPD", "noe", "Niederösterreich", (48.20, 15.70)),
    ("ooe",  "Policija Zg. Avstrija · LPD", "ooe", "Oberösterreich", (48.20, 14.00)),
    # Southern Burgenland meets Slovenia at the Kalch tripoint near Bad
    # Radkersburg; same BMI feed schema as the four above.
    ("bgld", "Policija Gradiščanska · LPD", "bgld", "Burgenland", (47.846, 16.526)),
]
AT_POLICE_SKIP = re.compile(
    r"Betrug|Betrüger|Einbruch|Einbrecher|Diebstahl|Dieb|gestohlen|Raub|Räuber|Raufhandel|Schläger|"
    r"Drogen|Suchtgift|Suchtmittel|Festnahme|festgenommen|Täter|Fahndung|Sachbeschädigung|"
    r"Körperverletzung|Schlepper|Waffe|sexuell|Missbrauch|Ermittl|geklärt|ausgeforscht|angezeigt|"
    r"Kontrolle|Schwerpunkt|Vandal|Bedrohung|Erpress|Cyber|Phishing|Trickbetr|Enkeltrick|"
    r"Haftbefehl|Verhaftung|Anzeige|Prävention|Warnung|Kampagne|Ehrung|Verabschiedung|Vorstellung|"
    r"Ausbildung|Übergabe|Amtsantritt|Jubiläum", re.I)
AT_POLICE_CATCODE = [
    (r"Verkehrsunfall|Kollision|Unfall|kollidiert|prallt|überschlag|Motorrad|Frontal|abgekommen|angefahren", "accident"),
    (r"Brand|Feuer|Explosion|Gasaustritt|Rauch", "fire"),
    (r"Rettung|Notlage|vermisst|Vermisst|Suchaktion|Suche nach|abgestürzt|Absturz|Lawine|Bergung|"
     r"Bergsteiger|Wanderer|ertrunken|Badeunfall|eingeklemmt|verschüttet", "rescue"),
    (r"Arbeitsunfall|Forstunfall|schwer verletzt|Notarzt|Hubschrauber|reanim|verstorben|tot aufgefunden|"
     r"Leiche|Todesfall", "ems"),
]
_POL_STMK_HEAD = re.compile(r"^\s*([^|]{2,40})\|\s*([A-ZÄÖÜ][\wäöüß.\- ]{1,50}?)\s*[.–-]+\s*")


def make_at_police(code, label, region, state, fallback):
    """One fetcher per Landespolizeidirektion press feed (bmi.gv.at/rss/<st>_presse.xml)."""
    url = f"https://www.bmi.gv.at/rss/{code}_presse.xml"
    page = f"https://www.polizei.gv.at/{code}/presse/aussendungen/presse.html"

    def fetch_source():
        out = []
        text, status = fetch(url)
        if text is None:
            return out, status
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return out, "error: bad XML"
        cutoff = (datetime.now(LOCAL) - timedelta(days=AT_REPORT_MAX_AGE_DAYS)).strftime("%Y-%m-%d")
        for it in root.findall(".//item"):
            title = tidy(strip_tags(it.findtext("title") or ""))
            body = tidy(strip_tags(it.findtext("description") or ""))
            body = re.sub(r"^\s*Aktuelle Meldungen\s*Presseaussendung der Polizei \w+\s*", "", body)
            published = _parse_pubdate(it.findtext("pubDate") or "")
            if not title or published is None or AT_POLICE_SKIP.search(title):
                continue
            head = title + " " + body[:300]
            cat = next((c for pat, c in AT_POLICE_CATCODE if re.search(pat, head, re.I)), None)
            if cat is None:
                continue
            date, time_ = _at_report_when(body, published)
            if date < cutoff:
                continue
            # Styria opens with "District | Town. – …"; Carinthia names the place
            # in the title ("… in Klagenfurt", "… im Bezirk Völkermarkt").
            district = town = None
            m = _POL_STMK_HEAD.match(body)
            if m:
                district, town = tidy(m.group(1)), tidy(m.group(2))
                body = body[m.end():]
            if not town:
                scope = title + " " + body[:400]
                mg = re.search(r"\bGemeindegebiet von\s+((?:St\.|Sankt|Bad|Maria)\s+)?([A-ZÄÖÜ][\wäöüß.-]+"
                               r"(?:\s+(?:am|an der|im|ob|bei)\s+[A-ZÄÖÜ][\wäöüß-]+)?)", scope)
                mb = re.search(r"\bBezirk\s+([A-ZÄÖÜ][\wäöüß./-]+)", scope)
                town = (((mg.group(1) or "") + mg.group(2) if mg else None) or _at_report_town(title)
                        or (mb.group(1) if mb else None) or _at_report_town(body[:400]))
                if mb and not district:
                    district = mb.group(1)
            town = tidy(town).rstrip(".,") if town else None
            district = tidy(district).rstrip(".,") if district else None
            lat, lon = _at_geocode(town, state) if town else (None, None)
            if lat is None:
                lat, lon = fallback
            helpers = sorted({tidy(u) for u in re.findall(
                r"\b(?:FF|Feuerwehr|Bergrettung|Wasserrettung)\s+[A-ZÄÖÜ][\wäöüß-]+|Rettungshubschrauber(?:\s+C\s?\d+)?|"
                r"Christophorus\s?\d+|Rotes Kreuz|Rettung(?:sdienst)?|Notarzt|Alpinpolizei", body)})
            loc = town or district or state
            if district and town and district != town:
                loc = f"{town} ({district})"
            elif town:
                loc = f"{town} ({state})"
            out.append({
                "id": rowid("AT-POL-" + code, date, time_, title),
                "source": label, "region": region, "country": "AT",
                "ref": f"POL-{code.upper()}-{date[5:7]}{date[8:]}-{time_.replace(':', '')}",
                "date": date, "time": time_, "category": cat, "title": title[:120],
                "status": "closed", "location": loc, "lat": lat, "lon": lon,
                "units": ", ".join(["Polizei"] + helpers), "crew": None, "vehicles": None,
                "raw": body[:1600], "link": tidy(it.findtext("link") or "") or page,
            })
        return out, status

    return fetch_source


_STMK_LFV = "https://www.lfv.steiermark.at/"
_STMK_LIST_RE = re.compile(
    r'(\d{2})\.(\d{2})\.(\d{4})(?:(?!\d{2}\.\d{2}\.\d{4}).){0,400}?<a[^>]+href="([^"]*read-\d+/?)"[^>]*>(.*?)</a>', re.S)


# Styria's fire service runs one DotNetNuke CMS for the state association (LFV)
# and every Bereichsfeuerwehrverband (BFV). The LFV "Einsätze / Berichte aus
# den Bereichen" page only aggregates what a Bereich chooses to push up — by
# September 2026 that was Murau alone — while each BFV keeps its own, fuller
# "Einsätze" list. The five Bereiche on the Slovenian border are read
# directly; same list markup, same report pages, same parser.
#   (label, site base, list path, id prefix, ref prefix, article budget key,
#    fallback (lat, lon) for a report that names no town, or None)
AT_LFV_LISTS = [
    ("LFV Štajerska · poročila", _STMK_LFV, "Home/Aktuelles/Einsaetze-Berichte.aspx",
     "AT-STMK", "STMK-LFV", "stmk_lfv", None),
    ("BFV Leibnitz · poročila", "https://www.bfvlb.steiermark.at/", "desktopdefault.aspx/tabid-1963/",
     "AT-STMK-LB", "STMK-LB", "bfv_lb", (46.782, 15.545)),
    ("BFV Deutschlandsberg · poročila", "https://www.bfvdl.steiermark.at/", "desktopdefault.aspx/tabid-104/",
     "AT-STMK-DL", "STMK-DL", "bfv_dl", (46.815, 15.218)),
    ("BFV Radkersburg · poročila", "https://www.bfvra.steiermark.at/", "desktopdefault.aspx/tabid-871/",
     "AT-STMK-RA", "STMK-RA", "bfv_ra", (46.685, 15.985)),
    ("BFV Feldbach · poročila", "https://www.bfvfb.steiermark.at/", "desktopdefault.aspx/tabid-2368/",
     "AT-STMK-FB", "STMK-FB", "bfv_fb", (46.953, 15.888)),
    ("BFV Graz-Umgebung · poročila", "https://www.bfvgu.steiermark.at/", "desktopdefault.aspx/tabid-681/",
     "AT-STMK-GU", "STMK-GU", "bfv_gu", (47.071, 15.439)),
]


def make_at_lfv_list(label, base, path, id_prefix, ref_prefix, budget_key, fallback):
    """One fetcher per LFV/BFV Steiermark report list: dated title links, each
    with its own page whose text carries the alarm time and town. Only pages
    not yet stored are fetched (capped per cycle), so a poll costs a handful of
    requests."""

    def detail(html):
        m = re.search(r'<div class="detailnews">(.*?)</div>', html, re.S)
        if not m:
            return None                                   # not the report page: do not cache
        text = tidy(strip_tags(re.sub(r"<h[12][^>]*>.*?</h[12]>|<p class=\"author\">.*?</p>", " ",
                                      m.group(1), flags=re.S)))
        # The BFV pages open with the posting byline ("Erstellt von X am
        # 04.09.2026"); dropped so the call's own date in the narrative wins.
        text = re.sub(r"^\s*(?:[^.]{0,80}?\s)?Erstellt von .*? am \d{1,2}\.\d{1,2}\.\d{4}\s*", "", text)
        return text or None

    def fetch_source():
        out = []
        text, status = fetch(base + path)
        if text is None:
            return out, status
        cutoff = (datetime.now(LOCAL) - timedelta(days=AT_REPORT_MAX_AGE_DAYS)).strftime("%Y-%m-%d")
        seen = set()
        for dd, mo, yy, href, raw_title in _STMK_LIST_RE.findall(text):
            title = tidy(strip_tags(raw_title))
            date = f"{yy}-{mo}-{dd}"
            # "weiter lesen" is the second link of the same item on the BFV pages.
            if date < cutoff or not title or title.lower().startswith("weiter lesen"):
                continue
            cat = _at_report_category(title)
            if cat is None:
                continue
            link = href if href.startswith("http") else base + href.lstrip("/")
            if link in seen:
                continue
            seen.add(link)
            rid = rowid(id_prefix, date, "", title)
            # The report page is read once ever (article cache); a failed or
            # unusable fetch is not cached, so the row is completed on a later
            # cycle instead of staying without alarm time and town for good.
            body = fetch_article(link, detail, budget_key=budget_key, budget=10)
            if body:
                # The page date is the posting date; the text names the call's
                # own day ("Am Samstag, dem 29. August 2026, … um 19:57 Uhr").
                date, time_ = _at_report_when(body, datetime(int(yy), int(mo), int(dd), 12, 0, tzinfo=LOCAL))
                if not re.search(r"(?:um|gegen)\s+\d{1,2}[:.]\d{2}", body):
                    time_ = ""
                town = _at_report_town(title) or _at_report_town(body)
            else:
                body, time_ = title, ""
                town = _at_report_town(title)
            lat, lon = _at_geocode(town, "Steiermark") if town else (None, None)
            if lat is None and fallback:
                lat, lon = fallback
            location = f"{town} (Steiermark)" if town else (
                label.split(" ·")[0].replace("BFV ", "") + " (Steiermark)" if fallback else "Steiermark")
            out.append({
                "id": rid, "source": label, "region": "stmk", "country": "AT",
                "ref": f"{ref_prefix}-{mo}{dd}", "date": date, "time": time_ or "",
                "category": cat, "title": title[:120], "status": "closed",
                "location": location, "lat": lat, "lon": lon,
                "units": None, "crew": None, "vehicles": None, "raw": (body or "")[:1600], "link": link,
            })
        return out, status

    return fetch_source


# Kept under its old name: the statewide list is the first entry above.
src_at_stmk_lfv = make_at_lfv_list(*AT_LFV_LISTS[0])


def place_label(text: str):
    scrubbed = UNIT_RE.sub(" ", text)
    for m in LOC_PHRASE.finditer(scrubbed):
        hit = _match(m.group(1))
        if hit:
            return place_case(hit[0])
    low = scrubbed.lower()
    hits = [n for n in PLACES
            if re.search(r"(?<![\wšđčćž])" + re.escape(n) + r"(?![\wšđčćž])", low)]
    return place_case(max(hits, key=len)) if hits else None


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


# ── ÖAMTC regional traffic feeds (per-item georss coordinates) ────────────
# The auto club's public traffic service carries the official EVIS.at data for
# the border-corridor motorways and B-roads (A2/A9/A11/S35, B67/B69/B70…). Most
# of the feed is roadworks and lane closures; we keep only genuine incidents.
AT_TRAFFIC = [
    ("ÖAMTC promet · Štajerska", "stmk", "https://www.oeamtc.at/feeds/verkehr/steiermark.xml", (47.070, 15.439)),
    ("ÖAMTC promet · Koroška",   "ktn",  "https://www.oeamtc.at/feeds/verkehr/kaernten.xml",   (46.624, 14.308)),
]
_AT_TRAFFIC_INCIDENT = re.compile(
    r"Unfall|Fahrzeugbrand|Fahrzeug in Brand|Brand|umgestürzt|umgekippt|Baum auf|"
    r"Bergung|geborgen|verletzt|Person|Ölspur|Hindernis|verlorene? Ladung|Tier auf", re.I)
_AT_TRAFFIC_SKIP = re.compile(
    r"Baustelle|Bauarbeiten|Belagsarbeiten|Sanierung|Wartung|Mäharbeiten|Reinigung|"
    r"Nachtsperre|Tagessperre|Instandhaltung|Markierungsarbeiten|Brückenarbeiten|"
    r"Tunnelwartung|Veranstaltung|Demonstration|Umleitung wegen Bau", re.I)


def make_at_traffic(label, region, url, fallback):
    """One fetcher per ÖAMTC regional traffic RSS. Incidents only — coordinates
    come from each item's <georss:point>."""

    def fetch_source():
        out = []
        text, status = fetch(url)
        if text is None:
            return out, status
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return out, "error: bad XML"
        geo = "{http://www.georss.org/georss}point"
        cutoff = (datetime.now(LOCAL) - timedelta(days=AT_MAX_AGE_DAYS)).strftime("%Y-%m-%d")
        for it in root.findall(".//item"):
            title = tidy(strip_tags(it.findtext("title") or ""))
            body = tidy(strip_tags(it.findtext("description") or ""))
            blob = f"{title} {body}"
            if _AT_TRAFFIC_SKIP.search(blob) or not _AT_TRAFFIC_INCIDENT.search(blob):
                continue
            published = _parse_pubdate(it.findtext("pubDate") or "")
            if published is None:
                continue
            date, time_ = published.strftime("%Y-%m-%d"), published.strftime("%H:%M")
            if date < cutoff:
                continue
            lat, lon = fallback
            pt = (it.findtext(geo) or "").split()
            if len(pt) == 2:
                try:
                    lat, lon = float(pt[0]), float(pt[1])
                except ValueError:
                    lat, lon = fallback
            out.append({
                "id": rowid(label, date, time_, title),
                "source": label, "region": region, "country": "AT",
                "ref": f"OEAMTC-{region.upper()}-{date[5:7]}{date[8:]}-{time_.replace(':', '')}",
                "date": date, "time": time_,
                "category": "fire" if re.search(r"brand|feuer", blob, re.I) else "accident",
                "status": "active", "title": title[:120],
                "location": title.split(",")[0][:80] + f" ({'Steiermark' if region == 'stmk' else 'Kärnten'})",
                "lat": lat, "lon": lon, "units": None, "crew": None, "vehicles": None,
                "raw": body[:1600], "link": tidy(it.findtext("link") or ""),
            })
        return out, status

    return fetch_source


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
    ("HAC · autoceste",   src_hac,           "HTML, national motorway network"),
    ("NÖ Feuerwehr · Wastl", src_at_noe,      "HTML, Lower Austria state dispatch"),
    ("OÖ Feuerwehr · LFV", src_at_ooe,        "HTML, Upper Austria state dispatch"),
    *[(label, make_at_lfv_list(label, base, path, idp, refp, bk, fb), "HTML list + report pages, Styria")
      for (label, base, path, idp, refp, bk, fb) in AT_LFV_LISTS],
    *[(label, make_at_feed(label, region, state, url, pfx, fb), "brigade report RSS")
      for (label, region, state, url, pfx, fb) in AT_FEEDS],
    *[(label, make_at_police(code, label, region, state, fb), "BMI police press RSS")
      for (code, label, region, state, fb) in AT_POLICE],
    *[(label, make_at_traffic(label, region, url, fb), "ÖAMTC georss traffic")
      for (label, region, url, fb) in AT_TRAFFIC],
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
  title_sl TEXT, raw_sl TEXT, country TEXT, status TEXT, cluster TEXT, hash TEXT,
  first_seen TEXT, last_seen TEXT);
CREATE INDEX IF NOT EXISTS ix_inc_date ON incidents(date DESC, time DESC);
CREATE TABLE IF NOT EXISTS sources(
  name TEXT PRIMARY KEY, method TEXT, status TEXT, count INTEGER, checked TEXT);
CREATE TABLE IF NOT EXISTS geocode_cache(
  q TEXT PRIMARY KEY, lat REAL, lon REAL, tried_at TEXT);
CREATE TABLE IF NOT EXISTS translate_cache(
  k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS meta(
  k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS article_cache(
  url TEXT PRIMARY KEY, fetched_at TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS http_validators(
  url TEXT PRIMARY KEY, etag TEXT, modified TEXT, saved_at TEXT);
CREATE TABLE IF NOT EXISTS live_start(
  id TEXT PRIMARY KEY, started TEXT, seen_at TEXT);
"""


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # A cache DB restored from a run that died mid-write can be unreadable; a
    # crash here would be re-saved and re-restored by every successor, so
    # check first and start cold instead (one slow cycle, not a dead chain).
    try:
        ok = conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    except sqlite3.DatabaseError:
        ok = False
    if not ok:
        conn.close()
        bad = DB_PATH.with_suffix(".corrupt")
        DB_PATH.replace(bad)
        print(f"  baza {DB_PATH.name} je pokvarjena – premaknjena v {bad.name}, začenjam na novo")
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # A cache DB restored from an older run predates some columns.
    for table, col in (("incidents", "title_sl TEXT"), ("incidents", "raw_sl TEXT"),
                       ("incidents", "country TEXT"), ("incidents", "status TEXT"),
                       ("incidents", "cluster TEXT"), ("incidents", "hash TEXT"),
                       ("geocode_cache", "tried_at TEXT")):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    # One-shot: the first translation run stored glossary/original fallbacks in
    # title_sl. Those "look done" and would block a real translation forever, so
    # clear them once — the border-report gibberish and the HR titles that only
    # echo the original — and let MyMemory redo them (cache-backed, so cheap).
    if not conn.execute("SELECT 1 FROM meta WHERE k='tsl_reset1'").fetchone():
        with conn:
            conn.execute("UPDATE incidents SET title_sl=NULL, raw_sl=NULL WHERE region IN ('stmk','ktn')")
            conn.execute("UPDATE incidents SET title_sl=NULL WHERE title_sl=title")
            conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('tsl_reset1','1')")
    # One-shot: Austrian geocode keys are case-normalised now ('AT|town|state'
    # with a lower-cased town); fold the old mixed-case entries in.
    if not conn.execute("SELECT 1 FROM meta WHERE k='geo_lower1'").fetchone():
        with conn:
            for row in conn.execute("SELECT q, lat, lon FROM geocode_cache WHERE q LIKE 'AT|%'").fetchall():
                parts = row["q"].split("|", 2)
                if len(parts) == 3 and parts[1] != parts[1].lower():
                    conn.execute("INSERT OR IGNORE INTO geocode_cache(q, lat, lon) VALUES(?,?,?)",
                                 (f"AT|{parts[1].lower()}|{parts[2]}", row["lat"], row["lon"]))
                    conn.execute("DELETE FROM geocode_cache WHERE q=?", (row["q"],))
            conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('geo_lower1','1')")
    return conn


_CONTENT_COLS = ("source", "region", "country", "ref", "date", "time", "category", "status",
                 "title", "location", "lat", "lon", "units", "crew", "vehicles", "raw", "link",
                 "title_sl", "raw_sl", "cluster")


def store(conn, rows):
    """Insert new rows; UPDATE the content of known ones when it changed (a
    corrected date, a closed status, a sharper fix, a translation) so the local
    DB is what gets pushed and what other parsers read back. Returns
    (new, changed)."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new = changed = 0
    sel = "SELECT " + ",".join(_CONTENT_COLS) + " FROM incidents WHERE id=?"
    # A content change clears the push hash: the row is "dirty" until Supabase
    # has it, even if the source goes quiet (304) before the next good push.
    upd = ("UPDATE incidents SET " + ",".join(f"{c}=:{c}" for c in _CONTENT_COLS)
           + ", last_seen=:now, hash=NULL WHERE id=:id")
    with conn:
        for r in rows:
            vals = {c: r.get(c) for c in _CONTENT_COLS}
            vals["country"] = vals["country"] or "HR"
            vals["status"] = vals["status"] or None
            cur = conn.execute(sel, (r["id"],)).fetchone()
            if cur is not None:
                # A translation missing from this parse must not wipe a stored one.
                for c in ("title_sl", "raw_sl"):
                    if vals[c] is None:
                        vals[c] = cur[c]
                if any(vals[c] != cur[c] for c in _CONTENT_COLS):
                    conn.execute(upd, {**vals, "now": now, "id": r["id"]})
                    changed += 1
                else:
                    conn.execute("UPDATE incidents SET last_seen=? WHERE id=?", (now, r["id"]))
                continue
            conn.execute(
                "INSERT INTO incidents(id," + ",".join(_CONTENT_COLS) + ",first_seen,last_seen) VALUES(:id,"
                + ",".join(f":{c}" for c in _CONTENT_COLS) + ",:fs,:fs)",
                {**vals, "id": r["id"], "fs": now})
            new += 1
    return new, changed


# Cadence tiers. FAST sources change minute to minute (dispatch logs, live
# brigade logs, police wires, newsroom feeds) and run every cycle; SLOW ones
# publish a few times a day and run every second cycle. A manual request and
# the first cycle of a job always run everything. Labels must match SOURCES.
SLOW_SOURCES = frozenset({
    "Policija · 20 PU", "HAC · autoceste", "Meteoalarm", "HGSS · spašavanje",
    "DVD Horvati", "VZ Međimurske ž.",
    *[label for (label, *_rest) in AT_LFV_LISTS],
    *[label for (label, *_rest) in AT_FEEDS],
})


def sources_for_cycle(full: bool):
    """The (name, fn, method) entries to run this cycle."""
    return [s for s in SOURCES if full or s[0] not in SLOW_SOURCES]


def _run_task(fn):
    t0 = time.time()
    try:
        rows, status = fn()
    except Exception as e:                                        # noqa: BLE001
        rows, status = [], f"error: {type(e).__name__}"
    return rows, status, int((time.time() - t0) * 1000)


def fetch_sources(full: bool = True):
    """Fetch every source due this cycle, concurrently (MAX_WORKERS threads,
    never two requests to one host at once — see fetch()). The twenty police
    administrations are separate hosts, so they run as twenty tasks under
    the one 'Policija · 20 PU' label. Returns an ordered list of
    (name, rows, status, ms) plus the per-PU results."""
    tasks = []                       # (group name, sub name, callable)
    for name, fn, _method in sources_for_cycle(full):
        if name == "Policija · 20 PU":
            for pu in POLICE_PUS:
                tasks.append((name, pu[2], (lambda p=pu: src_police_one(*p))))
        else:
            tasks.append((name, name, fn))
    # Heaviest first so the long tail does not start last.
    heavy = ("Policija · 20 PU", "NÖ Feuerwehr · Wastl", "OÖ Feuerwehr · LFV",
             "LFV Štajerska · poročila", "sibenik.in · kronika")
    tasks.sort(key=lambda t: (t[0] not in heavy, t[0]))
    results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(_run_task, fn): (group, sub) for group, sub, fn in tasks}
        for fut in futs:
            group, sub = futs[fut]
            results[(group, sub)] = fut.result()
    out, pu_results = [], []
    for name, _fn, _method in sources_for_cycle(full):
        if name == "Policija · 20 PU":
            rows, worst, ms = [], "ok", 0
            for pu in POLICE_PUS:
                r, st, t = results[(name, pu[2])]
                rows.extend(r)
                ms += t
                pu_results.append((pu[2], r, st, t))
                if st not in ("ok", "unchanged") and worst in ("ok", "unchanged"):
                    worst = st
                elif st == "unchanged" and worst == "ok" and not r:
                    worst = "unchanged"
            out.append((name, rows, worst, ms))
        else:
            rows, st, ms = results[(name, name)]
            out.append((name, rows, st, ms))
    return out, pu_results


def assign_clusters(rows):
    """Cross-source clustering key for HR rows: the same call reported by a
    brigade log, the county centre, the police and a newsroom gets one
    `cluster` so the console can fold the duplicates. Deliberately strict —
    same day, same category, within ~2 km, within a 45-minute bucket, and only
    when at least two *different* sources agree; if one source has two rows
    in the same bucket (two distinct fires in one village), the bucket is
    ambiguous and nobody in it is clustered."""
    buckets = {}
    for r in rows:
        r["cluster"] = None
        if r.get("country", "HR") != "HR" or r.get("lat") is None or not r.get("date"):
            continue
        if r.get("category") in ("summary", "weather", "other", "exercise"):
            continue
        t = r.get("time") or ""
        if not re.match(r"^\d{2}:\d{2}$", t):
            continue
        minutes = int(t[:2]) * 60 + int(t[3:])
        key = (r["date"], r["category"], round(r["lat"] / 0.02), round(r["lon"] / 0.03), minutes // 45)
        buckets.setdefault(key, []).append(r)
    n = 0
    for key, members in buckets.items():
        sources = [m["source"] for m in members]
        if len(set(sources)) < 2 or len(sources) != len(set(sources)):
            continue
        ck = hashlib.md5("|".join(map(str, key)).encode()).hexdigest()[:12]
        for m in members:
            m["cluster"] = ck
        n += len(members)
    return n


def _record_source(conn, name, method, status, ms):
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
            (name, method, f"{status} · {ms} ms", stored,
             datetime.now(timezone.utc).isoformat(timespec="seconds")))


def poll_once(conn, verbose=True, full=True, resync=False):
    """One cycle: fetch (concurrently), date-guard, geocode-refine, translate,
    store, delta-push, prune, report source health. Returns a dict with
    `new`, `changed`, `push_ok`, `seconds`. Never raises on a source or on
    Supabase; only a broken local DB can stop it."""
    t_cycle = time.time()
    _CYCLE["resync"] = resync
    _geo_budget[0] = NOMINATIM_MAX_PER_CYCLE
    _article_budget.clear()
    load_validators(conn)
    evict_caches(conn, verbose)
    methods = {name: method for name, _fn, method in SOURCES}
    phases = {}
    t0 = time.time()
    results, pu_results = fetch_sources(full)
    phases["viri"] = time.time() - t0
    total_new = total_changed = 0
    all_rows, statuses, refuted = [], [], {}
    for name, rows, status, ms in results:
        if rows:
            refuted[name] = defuture(rows)      # date guard first: everything below keys on the date
            all_rows.extend(rows)
    # Sharpen settlement-centroid fixes to an actual street where the text
    # names one, before storing — see refine_locations() docstring.
    t0 = time.time()
    try:
        refine_locations(conn, all_rows)
    except Exception as e:                                        # noqa: BLE001
        if verbose:
            print(f"  street refinement skipped: {type(e).__name__}: {e}")
    phases["ulice"] = time.time() - t0
    # Translate titles/narratives into Slovenian (cache-first, budgeted), so the
    # stored row and the push carry title_sl/raw_sl alongside the originals.
    t0 = time.time()
    try:
        mt = translate_rows(conn, all_rows)
    except Exception as e:                                        # noqa: BLE001
        mt = f"preskočeno: {type(e).__name__}"
    phases["prevod"] = time.time() - t0
    # Cluster against the stored recent rows too, or a fast cycle (without the
    # police and other SLOW partners) would strip clusters that the next full
    # cycle puts back — a content change every cycle for nothing.
    seen_ids = {r["id"] for r in all_rows}
    since = (datetime.now(LOCAL) - timedelta(days=3)).strftime("%Y-%m-%d")
    partners = [dict(r) for r in conn.execute(
        "SELECT id, source, country, date, time, category, lat, lon FROM incidents "
        "WHERE country='HR' AND date >= ? AND lat IS NOT NULL", (since,)) if r["id"] not in seen_ids]
    clustered = assign_clusters(all_rows + partners)
    t0 = time.time()
    for name, rows, status, ms in results:
        new, changed = store(conn, rows) if rows else (0, 0)
        total_new += new
        total_changed += changed
        _record_source(conn, name, methods.get(name, ""), status, ms)
        statuses.append((name, status, len(rows), ms))
        if verbose:
            rd = refuted.get(name, 0)
            print(f"  {name:28s} {status[:28]:28s} {len(rows):3d} vrstic, {new:3d} novih, "
                  f"{changed:3d} sprem.  {ms:6d} ms" + (f"  [{rd} predatiranih]" if rd else ""))
    # The police source fans out over twenty administrations under one name; record
    # each PU as its own source row so the dashboard can show per-county freshness.
    for label, rows, status, ms in pu_results:
        _record_source(conn, label, "gov.hr CMS", status, ms)
        statuses.append((label, status, len(rows), ms))
    phases["shranjevanje"] = time.time() - t0
    if verbose:
        print(f"  → {mt}; {clustered} vrstic v skupinah; Nominatim preostanek {_geo_budget[0]}")
    # Mirror the changed part of this cycle's parse into Supabase.
    t0 = time.time()
    sb_reset_cycle()
    sb, push_ok = push_supabase(conn, all_rows, resync=resync)
    if verbose and sb != "off":
        print(f"  {'→ Supabase':28s} {sb}")
    # Austria's retention rule is narrower than Croatia's: actually delete
    # anything past its window, every cycle, rather than keep a forever
    # archive. Runs after the push so nothing is deleted before it lands.
    if push_ok and sb != "off":
        pr = prune_supabase_austria()
        if verbose:
            print(f"  {'→ AT prune':28s} {pr}")
    st = push_source_status(statuses)
    if verbose and st != "off":
        print(f"  {'→ source_status':28s} {st}")
    phases["supabase"] = time.time() - t0
    seconds = time.time() - t_cycle
    if verbose:
        print(f"  cikel: {seconds:.1f} s ({', '.join(f'{k} {s:.1f} s' for k, s in phases.items())}), "
              f"{len(all_rows)} vrstic, {total_new} novih, {total_changed} spremenjenih"
              + ("" if push_ok else " — Supabase ne odgovarja"))
    return {"new": total_new, "changed": total_changed, "push_ok": push_ok, "seconds": seconds,
            "rows": len(all_rows)}


def poller(conn, interval, stop):
    # main() has just polled everything; wait a full interval before the first
    # background cycle rather than hitting every source twice inside ten seconds.
    stop.wait(interval)
    cycle = 1
    while not stop.is_set():
        print(f"[{datetime.now(LOCAL):%H:%M:%S}] polling {len(SOURCES)} sources…")
        try:
            res = poll_once(conn, full=cycle % 2 == 0)
            print(f"[{datetime.now(LOCAL):%H:%M:%S}] {res['new']} new incident(s) stored")
        except Exception as e:                                # noqa: BLE001
            print(f"  poll failed: {e}")
        cycle += 1
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
            now = datetime.now(LOCAL)
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
            res = poll_once(self.conn, verbose=False)
            return self._send(json.dumps({"new": res["new"]}).encode(), "application/json")
        self._send(b"not found", "text/plain", 404)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8713)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--interval", type=int, default=600,
                    help="seconds between polls (default 600; please don't go below 300)")
    ap.add_argument("--once", action="store_true", help="fetch once, report, exit")
    ap.add_argument("--serve-minutes", type=int, default=0, metavar="N",
                    help="stay up for N minutes polling every --interval seconds and "
                         "honouring on-demand requests from the dashboard, then exit "
                         "(used by CI: GitHub's */15 cron drops most of its slots)")
    args = ap.parse_args()

    conn = db()
    print(f"VatroCAD · database {DB_PATH}")
    if args.serve_minutes:
        serve_loop(conn, args.serve_minutes, max(args.interval, 300))
        return
    print("Initial fetch…")
    res = poll_once(conn, full=True, resync=True)
    total = conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
    print(f"Stored incidents: {total} ({res['seconds']:.1f} s cycle)")
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
