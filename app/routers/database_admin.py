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

from .. import backup, database, dbconfig, reporting
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
def reading_access(_: User = Depends(require_admin)):
    """How to read the data from outside the app (Power BI, Excel, SQL)."""
    active = dbconfig.describe_url(database.active_url)
    postgres = active["driver"].startswith("postgresql")
    # the bundled database is only reachable inside docker ("db"); from
    # outside it is the server's own address on the published port
    bundled = database.active_source == "environment" and active["host"] in ("db", "localhost", "127.0.0.1")
    return {
        "postgres": postgres,
        "bundled": bundled,
        "host": None if bundled else active["host"],
        "port": (settings.powerbi_db_port or None) if bundled else active["port"],
        "database": active["database"],
        "reader": settings.powerbi_user if reporting.reader_configured(database.engine) else None,
        "views": [{"name": n, "about": about} for n, (about, _) in reporting.VIEWS.items()],
    }


@router.post("/reading/password")
def reading_password(_: User = Depends(require_admin)):
    """The read-only login's password (from POWERBI_PASSWORD), on request."""
    if not reporting.reader_configured(database.engine):
        raise HTTPException(404, "No read-only login is set up (POWERBI_PASSWORD is empty)")
    return {"user": settings.powerbi_user, "password": settings.powerbi_password}


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

