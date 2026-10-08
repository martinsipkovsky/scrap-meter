"""Muted alerts: a station whose alerts are muted gets no notifications (no
scrap, fail count, disconnect, production or job change alerts).

* "!mute Line 1" in a WhatsApp group mutes them until the station's job
  changes (or until someone turns them on in the web app); "!unmute Line 1"
  turns them on again. See app.commands.
* The Scrap warnings switch on the station view mutes them until someone
  turns them on again.

Every mute and unmute is kept as a StationEvent, listed in the Data log.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from .models import Station, StationEvent, utcnow

log = logging.getLogger("cognex.mute")


def mute(db: Session, station: Station, by: str | None, source: str, until_job_change: bool) -> None:
    station.alerts_muted = True
    station.muted_by = (by or "")[:120] or None
    station.muted_at = utcnow()
    station.muted_until_job_change = until_job_change
    db.add(StationEvent(station_id=station.id, kind="mute", by=station.muted_by, source=source,
                        detail="until the job changes" if until_job_change else "until turned on again"))
    db.commit()
    log.info("alerts of station %s muted by %s (%s)", station.name, by, source)


def unmute(db: Session, station: Station, by: str | None, source: str, detail: str | None = None) -> None:
    if not station.alerts_muted:
        return
    station.alerts_muted = False
    station.muted_by = station.muted_at = None
    station.muted_until_job_change = False
    db.add(StationEvent(station_id=station.id, kind="unmute", by=(by or "")[:120] or None, source=source,
                        detail=detail))
    db.commit()
    log.info("alerts of station %s on again (%s)", station.name, source)


def job_changed(db: Session, station: Station, old: str | None, new: str | None) -> None:
    """A muted-until-job-change station changed job: its alerts are on again."""
    if station.alerts_muted and station.muted_until_job_change:
        unmute(db, station, None, "job change", f"job changed from '{old}' to '{new}'")


def describe(station: Station) -> dict:
    """The mute state for the dashboard and the station view."""
    return {
        "alerts_muted": bool(station.alerts_muted),
        "muted_by": station.muted_by,
        "muted_at": station.muted_at,
        "muted_until_job_change": bool(station.muted_until_job_change),
    }


def events(db: Session, station_id: int | None = None, limit: int = 100) -> list[dict]:
    q = db.query(StationEvent)
    if station_id is not None:
        q = q.filter(StationEvent.station_id == station_id)
    rows = q.order_by(StationEvent.created_at.desc(), StationEvent.id.desc()).limit(limit).all()
    return [{"id": e.id, "station_id": e.station_id, "kind": e.kind, "by": e.by, "source": e.source,
             "detail": e.detail, "created_at": e.created_at} for e in rows]


def find_station(db: Session, name: str) -> Station | None:
    """The station called ``name`` (any case), else the only one whose name
    contains it; None when there is no such station or several."""
    want = (name or "").strip().lower()
    if not want:
        return None
    stations = db.query(Station).order_by(Station.sort_order, Station.name).all()
    exact = [s for s in stations if s.name.lower() == want]
    if exact:
        return exact[0]
    part = [s for s in stations if want in s.name.lower()]
    return part[0] if len(part) == 1 else None
