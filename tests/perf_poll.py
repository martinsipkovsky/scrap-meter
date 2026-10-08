"""Time and count the SQL of one simulated device read (the poller's and
listeners' work per reading) on a seeded database. Not a test."""
from __future__ import annotations

import statistics
import time

from sqlalchemy import event

from app import notifications, poller
from app.database import SessionLocal, engine
from app.models import Device

queries = 0


@event.listens_for(engine, "before_cursor_execute")
def _count(*_):
    global queries
    queries += 1


def main() -> None:
    global queries
    db = SessionLocal()
    dev = db.query(Device).order_by(Device.id).first()
    times, counts = [], []
    for _ in range(200):
        queries = 0
        t = time.perf_counter()
        poller.poll_device_once(db, dev)
        times.append((time.perf_counter() - t) * 1000)
        counts.append(queries)
    print(f"device read: {statistics.median(times):.2f} ms, {statistics.median(counts)} SQL statements")
    times, counts = [], []
    for _ in range(20):
        queries = 0
        t = time.perf_counter()
        notifications.check_all_production(db)
        times.append((time.perf_counter() - t) * 1000)
        counts.append(queries)
    print(f"production check: {statistics.median(times):.2f} ms, {statistics.median(counts)} SQL statements")
    db.rollback()


if __name__ == "__main__":
    main()
