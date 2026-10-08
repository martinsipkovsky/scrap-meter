"""Dump the statistics and OEE figures of a seeded database (see perf_seed.py)
at a fixed moment, to compare two versions of the app: run it with each
version and diff the files. Not a test.
``PYTHONPATH=/srv python /srv/tests/perf_compare.py --now 2026-10-08T20:00:00+00:00 --out /srv/data/a.json``
"""
from __future__ import annotations

import argparse
import datetime as dt
import json

from app import oee, scrap_stats
from app.database import SessionLocal


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    now = dt.datetime.fromisoformat(a.now)
    db = SessionLocal()
    out = {}
    for hours in (1, 8, 24, 168, 24 * 28):
        out[f"oee {hours}"] = oee.compute(db, hours=hours, now=now)
    today = now.date()
    for tz in ("UTC", "Europe/Bratislava", "Asia/Kolkata", "Asia/Kathmandu", "America/New_York"):
        z = scrap_stats.zone(tz)
        for first, last in ((0, 0), (1, 0), (6, 0), (27, 0), (20, 19), (40, 0)):
            out[f"scrap {tz} {first}-{last}"] = scrap_stats.report(
                db, today - dt.timedelta(days=first), today - dt.timedelta(days=last), z)
    with open(a.out, "w") as f:
        json.dump(out, f, sort_keys=True, default=str, indent=0)
    print("written", len(out))


if __name__ == "__main__":
    main()
