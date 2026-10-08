"""Fill an EMPTY database with realistic history for performance checks.

Not a test: run it by hand in a throwaway stack's web container, e.g.
``PYTHONPATH=/srv python /srv/tests/perf_seed.py --stations 8 --days 28``. It makes one
simulated device and station per line, three jobs each with cycle times,
readings every few seconds through two shifts on weekdays (OK/NOK added per
read, as since 1.8), a few manual entries and excluded readings, the counter
states of the current jobs, and then the daily tables.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import random

from app import daily, jobs
from app.database import SessionLocal, engine
from app.models import CounterState, Device, Job, Reading, Station, utcnow
from app.stations import new_source


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stations", type=int, default=8)
    ap.add_argument("--days", type=int, default=28)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    rnd = random.Random(a.seed)
    db = SessionLocal()
    if db.query(Station).count():
        raise SystemExit("the database already has stations: use an empty one")
    now = utcnow().replace(microsecond=0)
    start_day = (now - dt.timedelta(days=a.days)).date()
    stations = []
    for i in range(1, a.stations + 1):
        dev = Device(name=f"Line {i} camera", host="127.0.0.1", port=23, protocol="simulator",
                     protocol_config={}, enabled=False, connected=True, last_poll_at=now,
                     last_values={"pass": 0, "fail": 0, "job": f"L{i}_A"})
        db.add(dev)
        db.flush()
        st = Station(name=f"Line {i}", sources=[new_source(dev.id, ok="pass", nok="fail", job="job")],
                     default_job=f"L{i}_A", idle_timeout_min=15, sort_order=i,
                     stats_default="exclude" if i == a.stations else "include")
        db.add(st)
        db.flush()
        for k, j in enumerate("ABC"):
            name = f"L{i}_{j}"
            job = Job(name=name)
            jobs.apply_cycle(job, round(rnd.uniform(4, 12), 1), [1, 2, 4][k])
            db.add(job)
        stations.append((st, dev))
    db.commit()

    buf = io.StringIO()
    cols = ("station_id", "device_id", "job_name", "raw_pass", "raw_fail", "raw_count", "total_pass",
            "total_fail", "extra", "excluded", "included", "in_production", "source_id", "ok_added",
            "nok_added", "manual", "created_at")
    n = 0
    last = {}
    for st, dev in stations:
        totals: dict[str, list[int]] = {}
        job = st.default_job
        t_last = None
        for d in range(a.days + 1):
            day = start_day + dt.timedelta(days=d)
            if day.weekday() >= 5:
                continue
            job = f"{st.default_job[:-1]}{'ABC'[d % 3]}"
            fail = rnd.uniform(0.01, 0.09)
            for shift_start, shift_end in ((5, 13), (13, 21)):
                t = dt.datetime.combine(day, dt.time(shift_start), tzinfo=dt.timezone.utc)
                end = dt.datetime.combine(day, dt.time(shift_end), tzinfo=dt.timezone.utc)
                while t < end and t < now:
                    if rnd.random() < 0.002:  # a stop of up to an hour
                        t += dt.timedelta(minutes=rnd.randint(16, 60))
                        continue
                    t += dt.timedelta(seconds=rnd.randint(4, 12))
                    ok, nok = (0, 1) if rnd.random() < fail else (1, 0)
                    tp = totals.setdefault(job, [0, 0])
                    tp[0] += ok
                    tp[1] += nok
                    excluded = rnd.random() < 0.001
                    buf.write("\t".join(map(str, (
                        st.id, dev.id, job, tp[0] % 10000, tp[1] % 10000, (tp[0] + tp[1]) % 10000,
                        tp[0], tp[1], "{}", "t" if excluded else "f", "f", "t", "s1", ok, nok, "f",
                        t.isoformat()))) + "\n")
                    n += 1
                    t_last = t
            if rnd.random() < 0.3:  # a manual entry or correction
                t = dt.datetime.combine(day, dt.time(12, 30), tzinfo=dt.timezone.utc)
                if t < now:
                    m_ok = rnd.choice([5, 10, -3])
                    buf.write("\t".join(map(str, (
                        st.id, "\\N", job, m_ok, 2, 0, 0, 0, "{}", "f", "f", "t", "\\N", "\\N", "\\N", "t",
                        t.isoformat()))) + "\n")
                    n += 1
        last[st.id] = (job, totals.get(job, [0, 0]), t_last)
    buf.seek(0)
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cur:
            with cur.copy(f"COPY readings ({', '.join(cols)}) FROM STDIN") as cp:
                while chunk := buf.read(1 << 20):
                    cp.write(chunk)
        raw.commit()
    finally:
        raw.close()

    for st, dev in stations:
        job, (tp, tf), t_last = last[st.id]
        st.current_job = job
        st.last_reading_at = t_last
        st.last_pass_change_at = t_last
        dev.current_job = job
        dev.last_values = {"pass": tp % 10000, "fail": tf % 10000, "job": job}
        db.add(CounterState(station_id=st.id, job_name=job, device_id=dev.id, total_pass=tp, total_fail=tf, total_count=tp + tf, is_active=True))
    db.commit()
    print("readings", n)
    print("daily", daily.refresh(db, rebuild=True))


if __name__ == "__main__":
    main()
