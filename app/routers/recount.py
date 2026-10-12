"""Recalculate counts from the raw readings (Database page, admins only):
a preview of what would change, and the correction itself, made only on an
explicit confirmation and after an automatic backup. See app.recount.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import backup, recount, scrap_stats
from ..database import get_db
from ..models import User
from .database_admin import require_admin

log = logging.getLogger("cognex.recount")

router = APIRouter(prefix="/api/database/recount", tags=["recount"])


@router.get("")
def preview(tz: str | None = None, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """What the correction would change per station and day; changes nothing."""
    return {**recount.preview(db, scrap_stats.zone(tz)), "last": backup.load_state().get("last_recount")}


class RecountRequest(BaseModel):
    confirm: bool = False


@router.post("")
def run(req: RecountRequest, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    if not req.confirm:
        raise HTTPException(400, "Confirm that the counts will be changed.")
    username = user.username
    with backup.lock:
        if not any(recount.plan(db, st).fixes for st in recount._stations(db)):
            raise HTTPException(400, "Nothing to correct.")
        db.rollback()  # end the read, so the backup sees a quiet session
        # the current data first, so the correction can be undone
        safety = backup.make_backup(backup.backups_dir(), suffix="before-recalculation")
        backup.prune(backup.backups_dir(), backup.KEEP_SAFETY)
        try:
            done = recount.apply(db)
        except Exception as exc:  # noqa: BLE001
            log.exception("recalculation failed")
            raise HTTPException(500, f"The recalculation failed: {exc}. The data from before it is in "
                                     f"{safety.name}.") from None
    log.warning("counts recalculated by %s: %s", username, done)
    last = {"at": backup.utcnow().isoformat(), "by": username, "readings": done["readings"],
            "ok": done["ok"], "nok": done["nok"], "safety": safety.name}
    backup.update_state(last_recount=last)
    return {**done, "safety_backup": safety.name, "last": last}
