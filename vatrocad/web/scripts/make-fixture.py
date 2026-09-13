#!/usr/bin/env python3
"""Build public/fixtures/incidents.json from a captured poller payload.

Usage:
  1. Run the poller once in a scratch copy and capture the exact Supabase
     payload it would push (monkey-patch push_supabase to dump its `payload`
     list plus first_seen from SQLite) into payload_full.json.
  2. python3 scripts/make-fixture.py payload_full.json public/fixtures/incidents.json

The fixture is a stratified sample (~150 rows) that covers every
(country, category, status) combination, every source, translated and
untranslated rows, and rows without coordinates. It is public data; nothing
is anonymised. The app re-bases its clock so the newest row is always
"12 minutes ago" – the file never goes stale for the smoke tests.
"""
import json
import random
import sys
from collections import defaultdict
from datetime import date, timedelta

src, dst = sys.argv[1], sys.argv[2]
rows = json.load(open(src, encoding="utf-8"))
random.seed(7)

today = date.fromisoformat(max(r["occurred"] for r in rows if r.get("occurred") and r["category"] != "weather"))
floor = (today - timedelta(days=7)).isoformat()
rows = [r for r in rows if r.get("occurred") and r["occurred"] >= floor]

by_combo = defaultdict(list)
for r in rows:
    by_combo[(r["country"], r["category"], r["status"])].append(r)

picked, seen = [], set()

def take(r):
    if r["id"] not in seen:
        seen.add(r["id"]); picked.append(r)

# 1) every combo gets up to 3 rows (newest first, then random)
for combo, lst in by_combo.items():
    lst.sort(key=lambda r: (r["occurred"], r.get("occurred_time") or ""), reverse=True)
    for r in lst[:2]:
        take(r)
    if len(lst) > 2:
        take(random.choice(lst[2:]))
# 2) every source represented
for r in rows:
    if r["source"] not in {p["source"] for p in picked}:
        take(r)
# 3) translated rows, rows without coordinates, rows with raw_sl
for pred in (lambda r: r.get("title_sl") and r["title_sl"] != r["title"],
             lambda r: r["lat"] is None,
             lambda r: r.get("raw_sl")):
    pool = [r for r in rows if pred(r) and r["id"] not in seen]
    random.shuffle(pool)
    for r in pool[:8]:
        take(r)
# 4) top up to ~150 with the newest remaining rows, capping exercises
rest = sorted((r for r in rows if r["id"] not in seen),
              key=lambda r: (r["occurred"], r.get("occurred_time") or ""), reverse=True)
ex = sum(1 for p in picked if p["category"] == "exercise")
for r in rest:
    if len(picked) >= 150:
        break
    if r["category"] == "exercise":
        if ex >= 12:
            continue
        ex += 1
    take(r)

picked.sort(key=lambda r: (r["occurred"], r.get("occurred_time") or ""), reverse=True)
cols = ["id", "source", "region", "country", "ref", "occurred", "occurred_time", "ts", "category", "status",
        "title", "location", "lat", "lon", "units", "crew", "vehicles", "raw", "link", "title_sl", "raw_sl",
        "first_seen", "last_seen"]
out = [{c: r.get(c) for c in cols} for r in picked]
json.dump(out, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
print(f"{len(out)} rows → {dst}")
