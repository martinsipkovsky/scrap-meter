"""Database page API (admins only): see which database is in use, test and
save a different PostgreSQL server, optionally copy the current data into it,
and restart the app so the new setting takes effect.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import backup, daily, database, dbconfig, powerbi_access, protocols, reporting
from ..protocols.tcp_listener import parse_port_range
from ..config import settings
from ..database import Base, get_db, make_engine
from ..dependencies import require_api_user
from ..models import User

log = logging.getLogger("cognex.database")

router = APIRouter(prefix="/api/database", tags=["database"])

_COPY_BATCH = 2000


def require_admin(user: User = Depends(require_api_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Only administrators can configure the database")
    return user


class DbSettings(BaseModel):
    host: str = Field(min_length=1)
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = Field(min_length=1)
    user: str = Field(min_length=1)
    # blank = keep the password already saved
    password: Optional[str] = None
    copy_data: bool = False


def _with_saved_password(cfg: DbSettings) -> dict:
    data = cfg.model_dump(exclude={"copy_data"})
    if not data.get("password"):
        saved = dbconfig.load() or {}
        data["password"] = saved.get("password")
    return data


@router.get("")
def status(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    saved = dbconfig.load()
    counts = {}
    for table in Base.metadata.sorted_tables:
        try:
            counts[table.name] = db.execute(select(func.count()).select_from(table)).scalar_one()
        except Exception:  # noqa: BLE001
            db.rollback()
            counts[table.name] = None
    return {
        "active": dbconfig.describe_url(database.active_url),
        "source": database.active_source,
        "fallback_error": database.fallback_error,
        "saved": None if not saved else {
            **{k: saved.get(k) for k in ("host", "port", "database", "user")},
            "has_password": bool(saved.get("password")),
        },
        "tables": counts,
    }


@router.get("/reading")
def reading_access(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """How to read the data from outside the app (Power BI, Excel, SQL)."""
    active = dbconfig.describe_url(database.active_url)
    postgres = active["driver"].startswith("postgresql")
    # the bundled database is only reachable inside docker ("db"); from
    # outside it is the server's own address on the published port
    bundled = database.active_source == "environment" and active["host"] in ("db", "localhost", "127.0.0.1")
    access = powerbi_access.load()
    switched_on = bool(access.get("enabled")) and postgres
    if switched_on:
        # through the app's own port (powerbi_access), on this server
        host, port = None, powerbi_access.forwarder.port
    else:
        host = None if bundled else active["host"]
        port = (settings.powerbi_db_port or None) if bundled else active["port"]
    lo, hi = parse_port_range(settings.listen_ports)
    return {
        "postgres": postgres,
        "bundled": bundled,
        "host": host,
        "port": port,
        "database": active["database"],
        "reader": settings.powerbi_user if (switched_on or reporting.reader_configured(database.engine)) else None,
        "access": {
            "enabled": switched_on, "port": int(access.get("port") or powerbi_access.default_port()),
            "running": powerbi_access.forwarder.running, "error": powerbi_access.forwarder.error if switched_on else None,
            "env": powerbi_access.env_configured(), "first_port": lo, "last_port": hi,
        },
        "daily": daily.status(db),
        "views": [{"name": n, "about": about} for n, (about, _) in reporting.VIEWS.items()],
    }


@router.post("/reading/password")
def reading_password(_: User = Depends(require_admin)):
    """The read-only login's password, on request."""
    pw = powerbi_access.password()
    if not pw or not (powerbi_access.load().get("enabled") or reporting.reader_configured(database.engine)):
        raise HTTPException(404, "No read-only login is set up: turn on Power BI access first")
    return {"user": settings.powerbi_user, "password": pw}


class AccessIn(BaseModel):
    enabled: bool
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    new_password: bool = False


@router.put("/reading/access")
def reading_access_switch(payload: AccessIn, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Turn Power BI access on or off: the read-only login and the port the
    app opens for it (one of the listener ports, already published)."""
    from ..models import Device

    if payload.enabled:
        if database.engine.dialect.name != "postgresql":
            raise HTTPException(400, "Reports need PostgreSQL; the app uses a local SQLite file.")
        port = payload.port or int(powerbi_access.load().get("port") or powerbi_access.default_port())
        lo, hi = parse_port_range(settings.listen_ports)
        if not lo <= port <= hi:
            raise HTTPException(400, f"The port must be one of the listener ports {lo}-{hi} (LISTEN_PORTS), "
                                     "which the server already publishes.")
        clash = db.query(Device).filter(Device.protocol.in_(protocols.push_keys()), Device.port == port).first()
        if clash:
            raise HTTPException(409, f"Port {port} is used by device '{clash.name}'. Pick another one.")
        powerbi_access.save(True, port, payload.new_password)
    else:
        powerbi_access.save(False)
    error = powerbi_access.apply(database.engine, database.active_url)
    if error:
        raise HTTPException(400, error)
    return reading_access(db)


class DailyTz(BaseModel):
    timezone: str = Field(min_length=1, max_length=64)


@router.put("/reading/daily")
def daily_timezone(payload: DailyTz, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """The time zone of the daily data's days; changing it computes them again."""
    from zoneinfo import ZoneInfo

    try:
        ZoneInfo(payload.timezone)
    except Exception:  # noqa: BLE001 - unknown or malformed name
        raise HTTPException(400, f"Unknown time zone '{payload.timezone}'") from None
    daily.set_timezone(payload.timezone)
    daily.refresh(db)
    return daily.status(db)


@router.post("/reading/daily/rebuild")
def daily_rebuild(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Compute every day's rows again (after changes to old readings)."""
    result = daily.refresh(db, rebuild=True)
    return {**result, **daily.status(db)}


@router.post("/test")
def test_connection(cfg: DbSettings, _: User = Depends(require_admin)):
    ok, err = dbconfig.test_url(dbconfig.build_url(_with_saved_password(cfg)))
    return {"ok": ok, "error": err}


def copy_all_data(target_url: str) -> dict:
    """Copy every table of the current database into an empty target database."""
    target = make_engine(target_url)
    try:
        Base.metadata.create_all(bind=target)
        database.migrate_schema(target)
        with target.connect() as conn:
            if conn.execute(select(func.count()).select_from(Base.metadata.tables["users"])).scalar_one():
                raise HTTPException(
                    409,
                    "The new database already has data in it. Copy only works into an empty "
                    "database; save without copying to use the data that is already there.",
                )
        copied = {}
        with database.engine.connect() as src, target.begin() as dst:
            for table in Base.metadata.sorted_tables:
                n = 0
                result = src.execution_options(stream_results=True).execute(select(table))
                while batch := result.fetchmany(_COPY_BATCH):
                    dst.execute(table.insert(), [dict(r._mapping) for r in batch])
                    n += len(batch)
                copied[table.name] = n
            backup.reset_sequences(dst)
        return copied
    finally:
        target.dispose()


@router.put("")
def save_settings(cfg: DbSettings, _: User = Depends(require_admin)):
    data = _with_saved_password(cfg)
    url = dbconfig.build_url(data)
    ok, err = dbconfig.test_url(url)
    if not ok:
        raise HTTPException(400, f"Cannot connect to the new database, nothing was saved: {err}")
    copied = copy_all_data(url) if cfg.copy_data else None
    dbconfig.save(data)
    return {"saved": True, "copied": copied, "restart_required": True}


@router.delete("")
def reset_settings(_: User = Depends(require_admin)):
    """Forget the saved database; after restart DATABASE_URL is used again."""
    dbconfig.clear()
    return {"saved": False, "restart_required": True}


@router.post("/restart")
def restart(_: User = Depends(require_admin)):
    """Exit the process shortly after replying; docker's restart policy
    (restart: unless-stopped) starts it again with the saved database."""
    log.warning("restart requested from the Database page")
    threading.Timer(1.0, lambda: os._exit(3)).start()
    return {"restarting": True}

