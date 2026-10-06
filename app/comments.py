"""Station comments.

A comment keeps a snapshot of its station when it was written: the station's
name, current job (the jobs its sources run, joined with " + ") and the OK /
NOK / scrap as the dashboard shows it. The snapshot is never updated afterwards.

Reports read them through the powerbi_station_comments view (app.reporting).
"""
from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import stations
from .models import Station, StationComment


def add(db: Session, station: Station, body: str, author: str | None) -> StationComment:
    active = stations.shown(db, station)
    comment = StationComment(
        station_id=station.id,
        station_name=station.name,
        job_name=active.job_name if active else station.current_job,
        ok_count=active.shown_pass if active else 0,
        nok_count=active.shown_fail if active else 0,
        scrap_rate=round(active.shown_scrap_rate, 4) if active else 0.0,
        text=body,
        author=author,
    )
    db.add(comment)
    db.commit()
    db.refresh(comment)
    return comment


def out(c: StationComment) -> dict:
    return {
        "id": c.id,
        "station_id": c.station_id,
        "station_name": c.station_name,
        "job": c.job_name,
        "ok": c.ok_count,
        "nok": c.nok_count,
        "scrap_rate": c.scrap_rate,
        "text": c.text,
        "author": c.author,
        "created_at": c.created_at,
    }


def latest(db: Session, station_ids: list[int]) -> dict[int, StationComment]:
    """The newest comment of each station."""
    if not station_ids:
        return {}
    # comments are only ever added with the current time, so the highest id is the newest
    newest = (db.query(func.max(StationComment.id)).filter(StationComment.station_id.in_(station_ids))
              .group_by(StationComment.station_id))
    rows = db.query(StationComment).filter(StationComment.id.in_(newest)).all()
    return {c.station_id: c for c in rows}

