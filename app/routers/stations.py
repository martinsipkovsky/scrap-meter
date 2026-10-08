"""Station CRUD, production start/stop, the dashboard counter reset and
manual entries.

A station counts OK / NOK (and optionally a total and the job) from one or
more sources, each a device and some of its values; see app.stations.
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import hmi, production, stations
from ..database import get_db
from ..dependencies import require_permission
from ..models import CounterState, NotificationRule, Station, User, utcnow
from ..schemas import StationCreate, StationUpdate

router = APIRouter(prefix="/api/stations", tags=["stations"])


def station_out(db: Session, st: Station, devices: dict) -> dict:
    view = stations.sources_view(db, st, devices)
    return {
        "id": st.id,
        "name": st.name,
        "sources": view["sources"],
        "active_devices": view["active_devices"],
        "last_active": view["last_active"],
        "default_job": st.default_job,
        "current_job": st.current_job,
        "idle_timeout_min": st.idle_timeout_min,
        "stats_default": st.stats_default,
        "sort_order": st.sort_order,
        "hmi_windows": hmi.windows(st),
        "last_reading_at": st.last_reading_at,
        **stations.status(st, devices),
        **production.describe(st),
    }


def _get(db: Session, station_id: int) -> Station:
    st = db.get(Station, station_id)
    if st is None:
        raise HTTPException(404, "Station not found")
    return st


def _sources(db: Session, payload_sources) -> list[dict]:
    raw = ([s.model_dump() for s in payload_sources] if isinstance(payload_sources, list) else payload_sources.model_dump())
    try:
        return stations.check_sources(db, raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("")
def list_stations(db: Session = Depends(get_db), _: User = Depends(require_permission("view_dashboard"))):
    rows = db.query(Station).order_by(Station.sort_order, Station.name).all()
    devices = stations.devices_of(db, rows)
    return [station_out(db, st, devices) for st in rows]


@router.post("", status_code=201)
def create_station(
    payload: StationCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("manage_devices")),
):
    if db.query(Station).filter(Station.name == payload.name).first():
        raise HTTPException(409, "A station with that name already exists")
    st = Station(**payload.model_dump(exclude={"sources"}), sources=_sources(db, payload.sources))
    st.hmi_windows = payload.model_dump()["hmi_windows"] or None
    db.add(st)
    db.commit()
    db.refresh(st)
    return station_out(db, st, stations.devices_of(db, [st]))


@router.patch("/{station_id}")
def update_station(
    station_id: int,
    payload: StationUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("manage_devices")),
):
    st = _get(db, station_id)
    data = payload.model_dump(exclude_unset=True, exclude={"sources"})
    if "name" in data and db.query(Station).filter(Station.name == data["name"], Station.id != st.id).first():
        raise HTTPException(409, "A station with that name already exists")
    if payload.sources is not None:
        st.sources = _sources(db, payload.sources)
    if "hmi_windows" in data:
        data["hmi_windows"] = data["hmi_windows"] or None
    for key, value in data.items():
        setattr(st, key, value)
    db.commit()
    db.refresh(st)
    return station_out(db, st, stations.devices_of(db, [st]))


@router.delete("/{station_id}", status_code=204)
def delete_station(
    station_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("manage_devices")),
):
    """Delete a station with its counters and readings; its alert rules go too."""
    st = _get(db, station_id)
    db.query(NotificationRule).filter(NotificationRule.station_id == st.id).delete()
    db.delete(st)
    db.commit()


@router.get("/{station_id}/hmi/{index}/check")
async def check_hmi(
    station_id: int,
    index: int,
    origin: str = Query(max_length=300),
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("view_dashboard")),
):
    """Ask the device of the station's HMI window ``index`` (from 0) for its
    page from the server: whether it answers, and whether it lets a browser
    on ``origin`` (the address the app is opened at) show it in a window.
    Only the station's saved addresses are asked (see app.hmi)."""
    wins = hmi.windows(_get(db, station_id))
    if not 0 <= index < len(wins):
        raise HTTPException(404, "No such HMI window")
    try:
        hmi.check_url(origin)
    except ValueError as exc:
        raise HTTPException(400, "origin " + str(exc)) from exc
    return await hmi.check(wins[index]["url"], origin)


def _set_production(db: Session, station_id: int, running: bool) -> dict:
    st = _get(db, station_id)
    if running:
        production.start(st)
    else:
        production.stop(st)
    db.commit()
    return production.describe(st)


@router.post("/{station_id}/production/start")
def production_start(
    station_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("control_connections")),
):
    """Put the station back in production now (clears a manual stop)."""
    return _set_production(db, station_id, True)


@router.post("/{station_id}/production/stop")
def production_stop(
    station_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("control_connections")),
):
    """Mark the station as not in production until Start is pressed."""
    return _set_production(db, station_id, False)


@router.post("/{station_id}/counters/reset")
def reset_counters(
    station_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("control_connections")),
):
    """Start the counters shown on the dashboard from zero for the jobs the
    station runs now.

    Nothing is sent to the devices, and the job totals in the readings history
    and the scrap statistics stay as they are (see CounterState.reset_shown).
    """
    st = _get(db, station_id)
    states = stations.active_states(db, st)
    if not states:
        raise HTTPException(400, "This station has no counters yet")
    now = utcnow()
    for state in states:
        state.reset_shown(now)
    db.commit()
    return {"job_name": " + ".join(s.job_name for s in states), "reset_at": now}


@router.get("/{station_id}/counters")
def station_counters(
    station_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("view_data")),
):
    _get(db, station_id)
    states = (
        db.query(CounterState)
        .filter(CounterState.station_id == station_id)
        .order_by(CounterState.is_active.desc(), CounterState.updated_at.desc())
        .all()
    )
    return [
        {
            "job_name": s.job_name,
            # device counts plus manual entries
            "total_pass": s.all_pass,
            "total_fail": s.all_fail,
            "total_count": s.all_count,
            "manual_pass": s.manual_pass or 0,
            "manual_fail": s.manual_fail or 0,
            "scrap_rate": round(s.scrap_rate, 4),
            "is_active": s.is_active,
            "updated_at": s.updated_at,
        }
        for s in states
    ]


class ManualEntry(BaseModel):
    ok: int = Field(default=0, ge=0, le=10_000_000)
    nok: int = Field(default=0, ge=0, le=10_000_000)
    at: dt.datetime | None = None  # default: now
    job: str | None = Field(default=None, max_length=255)
    note: str | None = Field(default=None, max_length=1000)


def check_entry(payload: ManualEntry) -> None:
    if payload.ok == 0 and payload.nok == 0:
        raise HTTPException(400, "Enter at least one OK or NOK part")
    if payload.at is not None:
        at = payload.at if payload.at.tzinfo else payload.at.replace(tzinfo=dt.timezone.utc)
        if at > dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5):
            raise HTTPException(400, "The time of an entry can't be in the future")


@router.post("/{station_id}/entries", status_code=201)
def add_entry(
    station_id: int,
    payload: ManualEntry,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("manual_entry")),
):
    """OK / NOK parts entered by hand; they count like device data."""
    st = _get(db, station_id)
    check_entry(payload)
    r = stations.add_entry(db, st, payload.ok, payload.nok, payload.at, payload.job, payload.note, user.username)
    return {"id": r.id, "job": r.job_name, "ok": r.raw_pass, "nok": r.raw_fail, "at": r.created_at}
