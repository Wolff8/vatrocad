#!/usr/bin/env python3
"""Relay for pages that only an Austrian/Slovenian network can reach.

The Styrian fire service's live dispatch list
(https://einsatzuebersicht.lfv.steiermark.at/lfvasp/einsatzkarte/Liste_App_Public.html)
drops TCP connections from every cloud egress we tried (GitHub Actions in the
US, AWS Ireland, Frankfurt and Paris). A home connection in Austria or
Slovenia gets through. This script runs on such a machine (a Raspberry Pi, a
laptop that stays on, a NAS), fetches the pages every few minutes and stores
the raw HTML in Supabase table public.relay_pages, from which the GitHub
Actions poller reads and parses them.

Stdlib only. Run it once to test, then keep it running:

    export SUPABASE_URL=https://ofzwkanpjhdvlygrhbum.supabase.co
    export SUPABASE_SERVICE_KEY=<service_role key>      # never commit this
    python3 lfv_relay.py --once          # fetch once, print what happened
    python3 lfv_relay.py --interval 300  # loop, every 5 minutes

The service key lives only in your shell / systemd unit on that machine.
"""
import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
UA = "VatroCAD-relay/1.0 (+https://github.com/Wolff8/vatrocad)"

# What to relay. Add per-Bereich variants (?Bereich=DL, LB, RA, FB, GU …) if the
# statewide list turns out to be capped.
PAGES = [
    "https://einsatzuebersicht.lfv.steiermark.at/lfvasp/einsatzkarte/Liste_App_Public.html?Bereich=all",
]


def fetch(url: str, timeout: int = 25):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": "https://www.lfv.steiermark.at/",
        "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.status, raw.decode(charset, "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:2000].decode("utf-8", "replace")
    except Exception as e:                                        # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def push(url: str, status: int, body: str) -> str:
    if not (SUPABASE_URL and SUPABASE_KEY):
        return "SUPABASE_URL / SUPABASE_SERVICE_KEY not set – not pushed"
    payload = json.dumps([{
        "url": url, "status": status, "body": body[:400_000],
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "relay": socket.gethostname(),
    }]).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/relay_pages?on_conflict=url", data=payload, method="POST",
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                 "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"})
    try:
        with urllib.request.urlopen(req, timeout=20):
            return "pushed"
    except urllib.error.HTTPError as e:
        return f"push failed: HTTP {e.code} {e.read()[:200].decode('utf-8', 'replace')}"
    except Exception as e:                                        # noqa: BLE001
        return f"push failed: {type(e).__name__}: {e}"


def cycle() -> None:
    for url in PAGES:
        t0 = time.time()
        status, body = fetch(url)
        ms = int((time.time() - t0) * 1000)
        if status == 200:
            note = push(url, status, body)
        else:
            note = f"not pushed ({body[:120]})"
        print(f"[{datetime.now():%H:%M:%S}] {status} {len(body):7d} B {ms:5d} ms  {url.split('/')[-1][:40]}  {note}",
              flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="fetch and push once, then exit")
    ap.add_argument("--interval", type=int, default=300, help="seconds between fetches (default 300)")
    a = ap.parse_args()
    if a.once:
        cycle()
        return
    while True:
        try:
            cycle()
        except Exception as e:                                    # noqa: BLE001
            print(f"cycle failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        time.sleep(max(60, a.interval))


if __name__ == "__main__":
    main()
