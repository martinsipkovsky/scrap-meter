"""OEE over the last hours (the dashboard's bottom panel).

OEE = availability x performance x quality, per station and overall:

* availability: time in production / the whole window. There are no shifts
  or planned stops yet, so the window (24 h) is the planned time. Time in
  production is the time between consecutive readings while the station was
  in production (see app.production), a gap counting at most the station's
  idle timeout (longer gaps mean the device was not read).
* performance: ideal cycle time x parts made / time in production. Needs the
  station's ideal cycle time (Station.ideal_cycle_s); without it the station
  has no performance and no OEE, only availability and quality.
* quality: OK / (OK + NOK).

Parts are counted like Scrap statistics (app.scrap_stats.station_parts):
excluded readings and parts made while not in production are left out. The
overall figures and the OK / NOK totals cover the stations included in the
statistics (Station.stats_default); the overall OEE covers those of them
that have an ideal cycle time.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from . import production
from .models import Station, utcnow
from .scrap_stats import station_parts


def _ratio(a: float, b: float) -> float | None:
    return round(a / b, 4) if b else None


def station_figures(db: Session, st: Station, start: dt.datetime, end: dt.datetime) -> dict:
    cap = dt.timedelta(minutes=max(1, st.idle_timeout_min or production.DEFAULT_IDLE_TIMEOUT_MIN))
    window = (end - start).total_seconds()
    ok = nok = 0
    prod = dt.timedelta()
    for r in station_parts(db, st, start, end):
        if r["excluded"]:
            continue
        if r["in_production"]:
            if r["prev_t"] is not None:
                prod += min(r["t"] - max(r["prev_t"], start), cap)
            ok += r["ok"]
            nok += r["nok"]
    prod_s = min(prod.total_seconds(), window)
    parts = ok + nok
    availability = _ratio(prod_s, window)
    quality = _ratio(ok, parts)
    performance = _ratio(st.ideal_cycle_s * parts, prod_s) if st.ideal_cycle_s else None
    oee = (round(availability * performance * quality, 4)
           if None not in (availability, performance, quality) else None)
    return {
        "id": st.id, "name": st.name, "in_totals": not st.excluded_by_default,
        "ideal_cycle_s": st.ideal_cycle_s, "ok": ok, "nok": nok, "production_s": round(prod_s),
        "availability": availability, "performance": performance, "quality": quality, "oee": oee,
    }


def compute(db: Session, hours: float = 24, now: dt.datetime | None = None) -> dict:
    end = now or utcnow()
    start = end - dt.timedelta(hours=hours)
    window = (end - start).total_seconds()
    rows = [station_figures(db, st, start, end)
            for st in db.query(Station).order_by(Station.sort_order, Station.name).all()]
    inc = [r for r in rows if r["in_totals"]]
    with_ct = [r for r in inc if r["ideal_cycle_s"]]
    ok, nok = sum(r["ok"] for r in inc), sum(r["nok"] for r in inc)

    oee = {"availability": None, "performance": None, "quality": None, "oee": None}
    if with_ct:
        prod = sum(r["production_s"] for r in with_ct)
        ideal = sum(r["ideal_cycle_s"] * (r["ok"] + r["nok"]) for r in with_ct)
        c_ok = sum(r["ok"] for r in with_ct)
        c_parts = c_ok + sum(r["nok"] for r in with_ct)
        oee["availability"] = _ratio(prod, window * len(with_ct))
        oee["performance"] = _ratio(ideal, prod)
        oee["quality"] = _ratio(c_ok, c_parts)
        if None not in (oee["availability"], oee["performance"], oee["quality"]):
            oee["oee"] = round(oee["availability"] * oee["performance"] * oee["quality"], 4)
    elif inc:
        # no cycle times: what can be said without them
        oee["availability"] = _ratio(sum(r["production_s"] for r in inc), window * len(inc))
        oee["quality"] = _ratio(ok, ok + nok)
    return {
        "hours": hours,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "totals": {"ok": ok, "nok": nok, "total": ok + nok, "quality": _ratio(ok, ok + nok)},
        "overall": {**oee, "stations": len(with_ct),
                    "without_cycle_time": [r["name"] for r in inc if not r["ideal_cycle_s"]]},
        "stations": rows,
    }
