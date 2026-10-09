"""System tab API (administrators): live values and the graphs' history."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import system_info
from ..database import get_db
from ..models import User
from .database_admin import require_admin

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("")
def live(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return system_info.snapshot(db)


@router.get("/history")
def history(hours: float = 24, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return system_info.history(db, hours)
