"""OEE over the last hours (the dashboard's bottom panel).

OEE = availability x performance x quality, per station and overall:

* availability: time in production / the whole window. There are no shifts
  or planned stops yet, so the window (24 h) is the planned time. Time in
  production is the time between consecutive readings while the station was
  in production (see app.production), a gap counting at most the station's
  idle timeout (longer gaps mean the device was not read).
* performance: ideal time of the parts made / time in production. Cycle times
  are per job (app.jobs): each reading's parts are weighed with the cycle time
  of the job they were made under, so a station that changes job mid-day is
  weighed correctly. Parts of jobs without a cycle time, and the production
  time spent on them, are left out of performance, and those jobs are listed.
  A station with no parts of a job with a cycle time has no performance and
  no OEE, only availability and quality.
* quality: OK / (OK + NOK).

Parts are counted like Scrap statistics (app.scrap_stats.station_parts):
excluded readings and parts made while not in production are left out. The
overall figures and the OK / NOK totals cover the stations included in the
statistics (Station.stats_default); the overall OEE covers those of them
that made parts of a job with a cycle time, or whose current job has one
(a station standing idle on such a job counts with availability 0).
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from . import jobs, production, stations
from .models import Station, utcnow
from .scrap_stats import station_parts


def _ratio(a: float, b: float) -> float | None:
    return round(a / b, 4) if b else None


def _quality(ok: int, parts: int) -> float | None:
    """OK / parts; corrections (negative manual entries) can take either
    below zero, so it stays within 0 - 1 and is None without parts."""
    return round(min(max(ok / parts, 0.0), 1.0), 4) if parts > 0 else None


def station_figures(db: Session, st: Station, start: dt.datetime, end: dt.datetime,
                    cycles: dict[str, float] | None = None) -> dict:
    """``cycles``: {job: ideal seconds per piece} (read from the jobs when None)."""
    if cycles is None:
        cycles = jobs.cycle_times(db)
    cap = dt.timedelta(minutes=max(1, st.idle_timeout_min or production.DEFAULT_IDLE_TIMEOUT_MIN))
    window = (end - start).total_seconds()
    ok = nok = 0
    prod = dt.timedelta()
    timed = dt.timedelta()  # production time on jobs with a cycle time
    ideal = 0.0  # ideal seconds of the parts made on those jobs
    missing: set[str] = set()  # jobs without a cycle time that were produced
    for r in station_parts(db, st, start, end):
        if r["excluded"] or not r["in_production"]:
            continue
        ct = cycles.get(r["job"])
        if r["prev_t"] is not None:
            gap = min(r["t"] - max(r["prev_t"], start), cap)
            prod += gap
            if ct:
                timed += gap
        ok += r["ok"]
        nok += r["nok"]
        if ct:
            ideal += ct * (r["ok"] + r["nok"])
        elif r["ok"] + r["nok"]:
            missing.add(r["job"])
    prod_s = min(prod.total_seconds(), window)
    timed_s = min(timed.total_seconds(), window)
    parts = ok + nok
    ideal = max(ideal, 0.0)  # corrections may take more back than was made
    availability = _ratio(prod_s, window)
    quality = _quality(ok, parts)
    performance = _ratio(ideal, timed_s) if ideal else None
    oee = (round(availability * performance * quality, 4)
           if None not in (availability, performance, quality) else None)
    return {
        "id": st.id, "name": st.name, "in_totals": not st.excluded_by_default,
        "ok": ok, "nok": nok, "production_s": round(prod_s),
        "timed_production_s": round(timed_s), "ideal_s": round(ideal, 1),
        # in the overall OEE: parts of a job with a cycle time, or idle on one
        "covered": bool(ideal) or any(cycles.get(j) for j in stations.current_jobs(db, st) or [st.default_job]),
        "jobs_without_cycle_time": sorted(missing),
        "availability": availability, "performance": performance, "quality": quality, "oee": oee,
    }


def compute(db: Session, hours: float = 24, now: dt.datetime | None = None) -> dict:
    end = now or utcnow()
    start = end - dt.timedelta(hours=hours)
    window = (end - start).total_seconds()
    cycles = jobs.cycle_times(db)
    rows = [station_figures(db, st, start, end, cycles)
            for st in db.query(Station).order_by(Station.sort_order, Station.name).all()]
    inc = [r for r in rows if r["in_totals"]]
    with_ct = [r for r in inc if r["covered"]]
    ok, nok = sum(r["ok"] for r in inc), sum(r["nok"] for r in inc)

    oee = {"availability": None, "performance": None, "quality": None, "oee": None}
    if with_ct:
        prod = sum(r["production_s"] for r in with_ct)
        c_ok = sum(r["ok"] for r in with_ct)
        c_parts = c_ok + sum(r["nok"] for r in with_ct)
        oee["availability"] = _ratio(prod, window * len(with_ct))
        oee["performance"] = _ratio(sum(r["ideal_s"] for r in with_ct),
                                    sum(r["timed_production_s"] for r in with_ct))
        oee["quality"] = _quality(c_ok, c_parts)
        if None not in (oee["availability"], oee["performance"], oee["quality"]):
            oee["oee"] = round(oee["availability"] * oee["performance"] * oee["quality"], 4)
    elif inc:
        # no cycle times: what can be said without them
        oee["availability"] = _ratio(sum(r["production_s"] for r in inc), window * len(inc))
        oee["quality"] = _quality(ok, ok + nok)
    return {
        "hours": hours,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "totals": {"ok": ok, "nok": nok, "total": ok + nok, "quality": _quality(ok, ok + nok)},
        "overall": {**oee, "stations": len(with_ct),
                    "without_cycle_time": [r["name"] for r in inc if not r["covered"]],
                    "jobs_without_cycle_time": sorted({j for r in inc for j in r["jobs_without_cycle_time"]},
                                                      key=str.lower)},
        "stations": rows,
    }
