"""Data API: readings history (and excluding readings), the dashboard
summary with the OEE meter, the station view (OK/NOK over time) and the scrap
statistics."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import oee, production, scrap_stats, stations
from ..database import get_db
from ..dependencies import require_permission
from ..models import CounterState, Reading, Station, User, utcnow
from .stations import ManualEntry, check_entry

router = APIRouter(prefix="/api/data", tags=["data"])

# bucket sizes the OK/NOK chart may use, smallest first (seconds)
_BUCKETS = (60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400)
_MAX_BARS = 72


def _aware(t: dt.datetime) -> dt.datetime:
    return t.replace(tzinfo=dt.timezone.utc) if t.tzinfo is None else t


def _station_summary(db: Session, st: Station, devices: dict) -> dict:
    active = (
        db.query(CounterState)
        .filter(CounterState.station_id == st.id, CounterState.is_active.is_(True))
        .first()
    )
    online = stations.status(st, devices)
    return {
        "id": st.id,
        "name": st.name,
        "devices": sorted(devices[i].name for i in st.device_ids() if i in devices),
        "connected": online["connected"],
        "last_error": online["problem"],
        "current_job": st.current_job,
        "last_poll_at": st.last_reading_at,
        "stats_default": st.stats_default,
        "ideal_cycle_s": st.ideal_cycle_s,
        **production.describe(st),
        "active_job": None
        if active is None
        else {
            "job_name": active.job_name,
            # since the last reset from the dashboard (see CounterState)
            "total_pass": active.shown_pass,
            "total_fail": active.shown_fail,
            "total_count": active.shown_count,
            "scrap_rate": round(active.shown_scrap_rate, 4),
            "reset_at": _aware(active.reset_at) if active.reset_at else None,
        },
    }


def _stations(db: Session) -> list[Station]:
    return db.query(Station).order_by(Station.sort_order, Station.name).all()


@router.get("/summary")
def summary(db: Session = Depends(get_db), _: User = Depends(require_permission("view_dashboard"))):
    """Every station's dashboard block."""
    rows = _stations(db)
    devices = stations.devices_of(db, rows)
    return [_station_summary(db, st, devices) for st in rows]


@router.get("/oee")
def oee_last_24h(
    hours: float = Query(24, gt=0, le=24 * 31),
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("view_dashboard")),
):
    """OEE and total OK / NOK over the last 24 hours (dashboard bottom panel)."""
    return oee.compute(db, hours=hours)


def ok_nok_buckets(readings: list[Reading], start: dt.datetime, end: dt.datetime, bucket_s: int) -> list[dict]:
    """OK/NOK parts produced per time bucket.

    Each reading carries the running totals of its job at that moment, so the
    parts made between two consecutive readings of the same job are the
    difference of their totals. A job change starts a new baseline (0 parts).
    ``readings`` must be oldest first; the first one only serves as baseline.
    """
    n = max(1, int((end - start).total_seconds() // bucket_s) + 1)
    first = int(start.timestamp()) // bucket_s * bucket_s
    bars = [{"t": dt.datetime.fromtimestamp(first + i * bucket_s, dt.timezone.utc), "ok": 0, "nok": 0} for i in range(n)]
    prev = None
    for r in readings:
        if r.manual:  # entered by hand: its own parts
            idx = (int(_aware(r.created_at).timestamp()) - first) // bucket_s
            if 0 <= idx < n:
                bars[idx]["ok"] += r.raw_pass
                bars[idx]["nok"] += r.raw_fail
            continue
        if prev is not None and prev.job_name == r.job_name:
            d_ok = r.total_pass - prev.total_pass
            d_nok = r.total_fail - prev.total_fail
            idx = (int(_aware(r.created_at).timestamp()) - first) // bucket_s
            if 0 <= idx < n:
                bars[idx]["ok"] += max(d_ok, 0)
                bars[idx]["nok"] += max(d_nok, 0)
        prev = r
    return bars


@router.get("/stations/{station_id}")
@router.get("/devices/{station_id}", include_in_schema=False)  # 1.4 path (device ids became station ids)
def station_view(
    station_id: int,
    hours: float = Query(8, gt=0, le=24 * 31),
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("view_dashboard")),
):
    """Everything the station view needs: status, totals and OK/NOK over time."""
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(404, "Station not found")
    end = utcnow()
    start = end - dt.timedelta(hours=hours)
    window = (end - start).total_seconds()
    bucket_s = next((b for b in _BUCKETS if window / b <= _MAX_BARS), _BUCKETS[-1])

    q = db.query(Reading).filter(Reading.station_id == station_id)
    baseline = (q.filter(Reading.created_at < start, Reading.manual.isnot(True))
                .order_by(Reading.created_at.desc()).first())
    rows = q.filter(Reading.created_at >= start).order_by(Reading.created_at.asc(), Reading.id.asc()).all()
    bars = ok_nok_buckets(([baseline] if baseline else []) + rows, start, end, bucket_s)
    return {
        **_station_summary(db, station, stations.devices_of(db, [station])),
        "history": {
            "hours": hours,
            "bucket_seconds": bucket_s,
            "bars": bars,
            "ok": sum(b["ok"] for b in bars),
            "nok": sum(b["nok"] for b in bars),
        },
    }


@router.get("/readings")
def readings(
    station_id: int | None = None,
    excluded: bool | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("view_data")),
):
    q = db.query(Reading)
    if station_id is not None:
        q = q.filter(Reading.station_id == station_id)
    if excluded is not None:
        # left out by a user, or by the station's statistics default
        by_default = Reading.station_id.in_(_excluded_station_ids(db)) & Reading.included.isnot(True)
        is_out = Reading.excluded.is_(True) | by_default
        q = q.filter(is_out if excluded else ~is_out)
    rows = q.order_by(Reading.created_at.desc()).limit(min(limit, 1000)).all()
    out_ids = set(_excluded_station_ids(db))
    return [
        {
            "id": r.id,
            "station_id": r.station_id,
            "job_name": r.job_name,
            "raw_pass": r.raw_pass,
            "raw_fail": r.raw_fail,
            "total_pass": r.total_pass,
            "total_fail": r.total_fail,
            "excluded": bool(r.excluded),
            "manual": bool(r.manual),
            "note": r.note,
            "entered_by": r.entered_by,
            # the station is excluded from the statistics by default
            "station_excluded": r.station_id in out_ids,
            "included": bool(r.included),
            "in_production": r.in_production,
            "created_at": r.created_at,
        }
        for r in rows
    ]


class ExcludeOne(BaseModel):
    excluded: bool


class ExcludePeriod(BaseModel):
    excluded: bool
    start: dt.datetime
    end: dt.datetime
    station_id: int | None = None  # None = all stations


def _excluded_station_ids(db: Session) -> list[int]:
    return [i for (i,) in db.query(Station.id).filter(Station.stats_default == "exclude")]


@router.patch("/readings/{reading_id}")
def exclude_reading(
    reading_id: int,
    payload: ExcludeOne,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("exclude_readings")),
):
    """Leave one reading's parts out of the scrap statistics, or take them back
    (also when its station is excluded from the statistics by default)."""
    r = db.get(Reading, reading_id)
    if not r:
        raise HTTPException(404, "Reading not found")
    r.excluded = payload.excluded
    r.included = not payload.excluded and r.station is not None and r.station.excluded_by_default
    db.commit()
    return {"id": r.id, "excluded": r.excluded, "included": r.included}


@router.put("/readings/{reading_id}/entry")
def edit_entry(
    reading_id: int,
    payload: ManualEntry,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("manual_entry")),
):
    """Change a manual entry (parts, time, job, note)."""
    r = db.get(Reading, reading_id)
    if not r or not r.manual:
        raise HTTPException(404, "Manual entry not found")
    check_entry(payload)
    stations.update_entry(db, r, payload.ok, payload.nok, payload.at, payload.job, payload.note)
    return {"id": r.id}


@router.delete("/readings/{reading_id}", status_code=204)
def delete_entry(
    reading_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("manual_entry")),
):
    """Delete a manual entry (device readings can only be excluded)."""
    r = db.get(Reading, reading_id)
    if not r or not r.manual:
        raise HTTPException(404, "Manual entry not found")
    stations.delete_entry(db, r)


@router.post("/readings/exclude")
def exclude_period(
    payload: ExcludePeriod,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("exclude_readings")),
):
    """Exclude (or include) every reading in a time period. Including also
    counts readings of stations that are excluded from the statistics by default."""
    start, end = _aware(payload.start), _aware(payload.end)
    if end <= start:
        raise HTTPException(400, "The end must be after the start")
    q = db.query(Reading).filter(Reading.created_at >= start, Reading.created_at < end)
    if payload.station_id is not None:
        q = q.filter(Reading.station_id == payload.station_id)
    changed = q.update({Reading.excluded: payload.excluded, Reading.included: False}, synchronize_session=False)
    if not payload.excluded:
        q.filter(Reading.station_id.in_(_excluded_station_ids(db))).update(
            {Reading.included: True}, synchronize_session=False)
    db.commit()
    return {"changed": changed}


_MAX_RANGE_DAYS = 366


def _scrap_range(start: dt.date, end: dt.date, tz: str | None, db: Session) -> dict:
    if end < start:
        raise HTTPException(400, "The To date must not be before the From date")
    if (end - start).days >= _MAX_RANGE_DAYS:
        raise HTTPException(400, f"The range may cover at most {_MAX_RANGE_DAYS} days")
    return scrap_stats.compute(db, start, end, scrap_stats.zone(tz))


@router.get("/scrap")
def scrap(
    start: dt.date = Query(..., alias="from"),
    end: dt.date = Query(..., alias="to"),
    tz: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("view_data")),
):
    """Pass/fail/scrap for a date range: overall, per station, per job, per day."""
    return _scrap_range(start, end, tz, db)


@router.get("/scrap.xlsx")
def scrap_xlsx(
    start: dt.date = Query(..., alias="from"),
    end: dt.date = Query(..., alias="to"),
    tz: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("view_data")),
):
    stats = _scrap_range(start, end, tz, db)
    name = f"scrap_{stats['from']}_{stats['to']}.xlsx"
    return Response(
        scrap_stats.to_xlsx(stats),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
