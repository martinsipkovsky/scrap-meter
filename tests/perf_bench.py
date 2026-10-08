"""Time every page and GET API of a seeded database (see perf_seed.py).

Not a test: run it by hand in a throwaway stack's web container, e.g.
``PYTHONPATH=/srv python /srv/tests/perf_bench.py --runs 5 --out /srv/data/bench.json``.
Prints, per URL, the median time, the number of SQL statements and the
response size; ``--out`` also saves the responses' JSON so two versions can
be compared for identical numbers (``--compare old.json``).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import time

from fastapi.testclient import TestClient
from sqlalchemy import event

from app.auth import hash_password
from app.database import SessionLocal, engine
from app.main import app
from app.models import Station, User

USER, PASSWORD = "perf-bench", "perf-bench-pw-1"
# answers that move with the clock or the request (left out of --compare)
VOLATILE = {"from", "to", "last_poll_at", "now", "generated_at", "last_run", "age_s", "since_s"}

queries = 0


@event.listens_for(engine, "before_cursor_execute")
def _count(*_):
    global queries
    queries += 1


def urls(sid: int) -> list[str]:
    today = dt.date.today()
    week, month = today - dt.timedelta(days=6), today - dt.timedelta(days=27)
    pages = ["/", "/stations", f"/station/{sid}", "/devices", "/jobs", "/data", "/scrap", "/notifications",
             "/users", "/account", "/database", "/rawdb", "/chat", "/changelog"]
    api = ["/api/data/summary", "/api/data/oee?hours=24", "/api/data/oee?hours=168",
           f"/api/data/stations/{sid}", "/api/stations", "/api/devices", "/api/jobs",
           f"/api/data/stations/{sid}?hours=168", f"/api/stations/{sid}/counters", "/api/devices/values",
           "/api/data/events", f"/api/data/readings?station_id={sid}", "/api/data/readings?limit=500",
           f"/api/data/scrap?from={week}&to={today}", f"/api/data/scrap?from={month}&to={today}",
           f"/api/data/scrap.xlsx?from={week}&to={today}", "/api/rawdb/tables", "/api/rawdb/tables/readings/rows",
           "/api/notifications/logs", "/api/notifications/commands/log", "/api/chat/messages", "/api/database"]
    return pages + api


def strip(v):
    if isinstance(v, dict):
        return {k: strip(x) for k, x in v.items() if k not in VOLATILE}
    if isinstance(v, list):
        return [strip(x) for x in v]
    return v


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--out")
    ap.add_argument("--compare")
    ap.add_argument("--extra", nargs="*", default=[])
    a = ap.parse_args()
    global queries
    db = SessionLocal()
    if not db.query(User).filter_by(username=USER).first():
        db.add(User(username=USER, password_hash=hash_password(PASSWORD), is_admin=True, is_active=True,
                    permissions=[]))
        db.commit()
    sid = db.query(Station.id).order_by(Station.id).first()[0]
    db.close()
    saved = {}
    with TestClient(app) as c:
        r = c.post("/login", data={"username": USER, "password": PASSWORD}, follow_redirects=False)
        assert r.status_code in (302, 303), r.text
        print(f"{'url':48} {'ms':>8} {'sql':>6} {'kB':>8}")
        for u in urls(sid) + a.extra:
            times = []
            for _ in range(a.runs):
                queries = 0
                t0 = time.perf_counter()
                r = c.get(u)
                times.append((time.perf_counter() - t0) * 1000)
            ms = statistics.median(times)
            print(f"{u:48} {ms:8.1f} {queries:6d} {len(r.content) / 1024:8.1f}  {r.status_code}")
            if "json" in r.headers.get("content-type", ""):
                saved[u] = strip(r.json())
    if a.out:
        with open(a.out, "w") as f:
            json.dump(saved, f, sort_keys=True, default=str)
    if a.compare:
        with open(a.compare) as f:
            old = json.load(f)
        new = json.loads(json.dumps(saved, sort_keys=True, default=str))
        diff = [u for u in old if old[u] != new.get(u)]
        print("identical" if not diff else "DIFFERENT: " + ", ".join(diff))


if __name__ == "__main__":
    main()
