"""Daily data: everything a day brought, per station and per station and job,
kept in the daily_stations / daily_jobs tables for reports (the
powerbi_daily_* views, app.reporting).

* Days are calendar days in one time zone (the DAILY_KEY setting, chosen on
  the Database tab; the FTP backup's zone or UTC until then).
* Parts are counted like Scrap statistics (app.scrap_stats.station_parts):
  excluded readings and parts made while not in production are counted
  separately. Production time and ideal time are counted like the OEE meter
  (app.oee), so the views can work out availability, performance, quality
  and OEE per day. A station excluded from the statistics still gets its
  rows, with in_totals false.
* The scheduler fills them in: today every few minutes, past days once when
  they are over, and on the first run of a new day the last WEEK days again
  (late manual entries or excluded readings). The first run, and a change of
  time zone, computes every day since the first reading. "Rebuild" on the
  Database tab does the same on request.
* A day's rows are written for the stations that exist; rows of deleted
  stations stay as they are.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import jobs, production, settings_store
from .models import DailyJob, DailyStation, Reading, Station, StationComment, utcnow
from .scrap_stats import day_bounds, station_parts, zone

log = logging.getLogger("cognex.daily")

DAILY_KEY = "daily_data"  # {"timezone": ..., "computed_tz": ..., "last_day": "YYYY-MM-DD"}
INTERVAL_S = 300
WEEK = 7

_lock = threading.Lock()


# ---- settings ---------------------------------------------------------------
def _settings() -> dict:
    value = settings_store.load(DAILY_KEY)
    return value if isinstance(value, dict) else {}


def timezone_name() -> str:
    name = _settings().get("timezone")
    if not name:
        from .backup_ftp import load as ftp_settings

        name = ftp_settings().get("timezone") or "UTC"
    return name


def set_timezone(name: str) -> None:
    settings_store.save(DAILY_KEY, {**_settings(), "timezone": name})


def _note(**values) -> None:
    settings_store.save(DAILY_KEY, {**_settings(), **values})


# ---- one station and day ---------------------------------------------------
def station_day(db: Session, st: Station, day: dt.date, tz_name: str, cycles: dict[str, float],
                now: dt.datetime) -> tuple[dict, list[dict]] | None:
    """The station's row and its job rows for ``day``; None for a day that
    has not started yet."""
    tz = zone(tz_name)
    start, end = day_bounds(day, day, tz)
    if start >= now:
        return None
    complete = end <= now
    end = min(end, now)
    cap = dt.timedelta(minutes=max(1, st.idle_timeout_min or production.DEFAULT_IDLE_TIMEOUT_MIN))
    row = {"day": day, "station_id": st.id, "station_name": st.name, "timezone": tz_name,
           "window_s": round((end - start).total_seconds()), "production_s": 0.0, "timed_production_s": 0.0,
           "ideal_s": 0.0, "ok": 0, "nok": 0, "manual_ok": 0, "manual_nok": 0, "excluded_ok": 0,
           "excluded_nok": 0, "idle_ok": 0, "idle_nok": 0, "in_totals": not st.excluded_by_default,
           "readings": 0, "comments": 0, "jobs": "", "first_reading_at": None, "last_reading_at": None,
           "complete": complete}
    per_job: dict[str, dict] = {}
    for r in station_parts(db, st, start, end):
        if not r.get("manual"):
            row["readings"] += 1
        row["first_reading_at"] = row["first_reading_at"] or r["t"]
        row["last_reading_at"] = r["t"]
        ok, nok = r["ok"], r["nok"]
        if r["excluded"]:
            row["excluded_ok"] += ok
            row["excluded_nok"] += nok
            continue
        if not r["in_production"]:
            row["idle_ok"] += ok
            row["idle_nok"] += nok
            continue
        name = r["job"] or "—"
        ct = cycles.get(r["job"])
        j = per_job.setdefault(name, {"day": day, "station_id": st.id, "station_name": st.name, "job": name,
                                      "ok": 0, "nok": 0, "manual_ok": 0, "manual_nok": 0, "production_s": 0.0,
                                      "ideal_cycle_s": ct, "ideal_s": 0.0})
        if r["prev_t"] is not None:
            gap = min(r["t"] - max(r["prev_t"], start), cap).total_seconds()
            row["production_s"] += gap
            j["production_s"] += gap
            if ct:
                row["timed_production_s"] += gap
        for target in (row, j):
            target["ok"] += ok
            target["nok"] += nok
            if r.get("manual"):
                target["manual_ok"] += ok
                target["manual_nok"] += nok
            if ct:
                target["ideal_s"] += ct * (ok + nok)
    row["comments"] = (db.query(func.count(StationComment.id))
                       .filter(StationComment.station_id == st.id, StationComment.created_at >= start,
                               StationComment.created_at < end).scalar() or 0)
    window = row["window_s"]
    row["production_s"] = round(min(row["production_s"], window))
    row["timed_production_s"] = round(min(row["timed_production_s"], window))
    row["ideal_s"] = round(row["ideal_s"], 1)
    job_rows = []
    for j in per_job.values():
        j["production_s"] = round(min(j["production_s"], window))
        j["ideal_s"] = round(j["ideal_s"], 1)
        if j["ok"] or j["nok"] or j["production_s"]:  # not a bare baseline reading
            job_rows.append(j)
    row["jobs"] = " + ".join(sorted((j["job"] for j in job_rows), key=str.lower))
    return row, job_rows


def compute_days(db: Session, days: list[dt.date], tz_name: str | None = None,
                 now: dt.datetime | None = None) -> int:
    """(Re)write the rows of these days for every station. Returns the
    number of station rows written."""
    now = now or utcnow()
    tz_name = tz_name or timezone_name()
    cycles = jobs.cycle_times(db)
    written = 0
    stations = db.query(Station).order_by(Station.id).all()
    for day in days:
        ids = [st.id for st in stations]
        db.query(DailyJob).filter(DailyJob.day == day, DailyJob.station_id.in_(ids)).delete(synchronize_session=False)
        db.query(DailyStation).filter(DailyStation.day == day, DailyStation.station_id.in_(ids)).delete(
            synchronize_session=False)
        stamp = utcnow()
        for st in stations:
            result = station_day(db, st, day, tz_name, cycles, now)
            if result is None:
                continue
            row, job_rows = result
            db.add(DailyStation(**row, updated_at=stamp))
            db.add_all(DailyJob(**j, updated_at=stamp) for j in job_rows)
            written += 1
        db.commit()
    return written


def _first_day(db: Session, tz: dt.tzinfo) -> dt.date | None:
    first = db.query(func.min(Reading.created_at)).scalar()
    if first is None:
        return None
    first = first.replace(tzinfo=dt.timezone.utc) if first.tzinfo is None else first
    return first.astimezone(tz).date()


def _range(first: dt.date, last: dt.date) -> list[dt.date]:
    return [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]


def refresh(db: Session, now: dt.datetime | None = None, rebuild: bool = False) -> dict:
    """Bring the daily rows up to date (see the module doc). Returns what was
    done."""
    with _lock:
        now = now or utcnow()
        tz_name = timezone_name()
        tz = zone(tz_name)
        today = now.astimezone(tz).date()
        saved = _settings()
        first = _first_day(db, tz)
        if first is None:
            return {"days": 0, "rows": 0}
        if rebuild or saved.get("computed_tz") != tz_name:
            # everything again (first run, other time zone): old rows were days
            # of another zone
            db.query(DailyJob).delete(synchronize_session=False)
            db.query(DailyStation).delete(synchronize_session=False)
            db.commit()
            days = _range(first, today)
        else:
            days = {today}
            # days not yet computed as complete (the app was off, or it's a new day)
            done = {d for (d,) in db.query(DailyStation.day).filter(DailyStation.complete.is_(True)).distinct()}
            last = saved.get("last_day")
            since = dt.date.fromisoformat(last) if last else first
            days |= {d for d in _range(max(first, since), today) if d not in done}
            if last != today.isoformat():
                # a new day: the last week again, for late changes
                days |= set(_range(max(first, today - dt.timedelta(days=WEEK)), today))
            days = sorted(days)
        rows = compute_days(db, days, tz_name, now)
        _note(computed_tz=tz_name, last_day=today.isoformat(), last_run=now.isoformat())
        return {"days": len(days), "rows": rows}


def status(db: Session) -> dict:
    saved = _settings()
    first, last = db.query(func.min(DailyStation.day), func.max(DailyStation.day)).one()
    return {"timezone": timezone_name(), "computed_tz": saved.get("computed_tz"), "last_run": saved.get("last_run"),
            "first_day": first.isoformat() if first else None, "last_day": last.isoformat() if last else None,
            "rows": db.query(func.count(DailyStation.id)).scalar() or 0}


# ---- scheduler ----------------------------------------------------------------
class Scheduler:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="cognex-daily", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        delay = 20  # first run shortly after startup
        while not self._stop.wait(delay):
            delay = INTERVAL_S
            from .database import SessionLocal

            db = SessionLocal()
            try:
                refresh(db)
            except Exception:  # noqa: BLE001 - never let the loop die
                log.exception("daily data failed")
                db.rollback()
            finally:
                db.close()


scheduler = Scheduler()
