"""Raw data tab API (admins only): the app's tables, their rows, logged
changes with undo, and a read-only SQL box. See app.rawdb."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from .. import rawdb
from ..database import get_db
from ..models import User
from .database_admin import require_admin

router = APIRouter(prefix="/api/rawdb", tags=["raw data"])


class RowValues(BaseModel):
    values: dict[str, Any] = {}


class SqlIn(BaseModel):
    query: str = Field(max_length=20_000)


def _run(db: Session, fn, *args):
    try:
        return fn(db, *args)
    except rawdb.RawError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        msg = str(exc.orig).strip().splitlines()[0]
        raise HTTPException(409, f"The database refused it: {msg}") from exc
    except SQLAlchemyError as exc:
        db.rollback()
        msg = str(getattr(exc, "orig", exc)).strip().splitlines()[0]
        raise HTTPException(400, f"The database refused it: {msg}") from exc


@router.get("/tables")
def list_tables(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return rawdb.tables(db)


@router.get("/tables/{name}/rows")
def table_rows(
    name: str,
    page: int = Query(1, ge=1),
    size: int = Query(50),
    sort: str | None = None,
    desc: bool = False,
    q: str | None = Query(None, max_length=200),
    filters: str | None = Query(None, max_length=5000, description='JSON list of {"column", "op", "value"}'),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    try:
        flt = json.loads(filters) if filters else []
        if not isinstance(flt, list):
            raise ValueError
    except ValueError:
        raise HTTPException(400, "filters must be a JSON list") from None
    return _run(db, rawdb.rows, name, page, size, sort, desc, q, flt)


@router.get("/tables/{name}/options/{column}")
def fk_options(name: str, column: str, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """The rows a foreign-key column can point to, for its dropdown."""
    return _run(db, rawdb.fk_options, name, column)


@router.patch("/tables/{name}/rows/{key}")
def update_row(name: str, key: str, payload: RowValues, db: Session = Depends(get_db),
               user: User = Depends(require_admin)):
    return _run(db, rawdb.update_row, user.username, name, key, payload.values)


@router.post("/tables/{name}/rows", status_code=201)
def insert_row(name: str, payload: RowValues, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return _run(db, rawdb.insert_row, user.username, name, payload.values)


@router.delete("/tables/{name}/rows/{key}", status_code=204)
def delete_row(name: str, key: str, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    _run(db, rawdb.delete_row, user.username, name, key, user.id)


@router.get("/audit")
def audit(limit: int = Query(100, ge=1, le=1000), table: str | None = None, db: Session = Depends(get_db),
          _: User = Depends(require_admin)):
    return rawdb.audit(db, limit, table)


@router.post("/audit/{entry_id}/undo")
def undo(entry_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return _run(db, rawdb.undo, user.username, entry_id)


@router.post("/sql")
def run_sql(payload: SqlIn, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """One read-only SELECT; at most rawdb.MAX_SQL_ROWS rows."""
    return _run(db, rawdb.run_select, payload.query)
