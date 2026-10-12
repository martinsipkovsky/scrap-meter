"""Backups on the Database page (admins only): download a backup of all data,
import one (replacing the current data, after an automatic safety backup),
and the automatic FTP backup settings.
"""
from __future__ import annotations

import logging
import secrets
import shutil
import time
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from .. import backup, backup_ftp
from ..auth import SESSION_COOKIE
from ..config import settings
from ..database import get_db
from ..models import User
from .database_admin import require_admin

log = logging.getLogger("cognex.backup")

router = APIRouter(prefix="/api/database/backup", tags=["backup"])

# uploaded files waiting for the admin to confirm the import
_UPLOAD_PREFIX = "upload-"
_UPLOAD_MAX_AGE = 3600


def _uploads_dir() -> Path:
    path = backup.backups_dir() / "uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ftp_public(cfg: dict) -> dict:
    out = {k: v for k, v in cfg.items() if k != "password"}
    out["has_password"] = bool(cfg.get("password"))
    return out


@router.get("")
def status(_: User = Depends(require_admin)):
    state = backup.load_state()
    cfg = backup_ftp.load()
    nxt = backup_ftp.next_run(cfg, backup_ftp.last_attempt(), backup.utcnow())
    return {
        "last_backup": state.get("last_backup"),
        "ftp": _ftp_public(cfg),
        "ftp_last_run": state.get("ftp_last_run"),
        "ftp_next_run": nxt.isoformat() if nxt else None,
        "safety_backups": backup.safety_backups(),
        "last_import": state.get("last_import"),
        "last_recount": state.get("last_recount"),
    }


@router.get("/download")
def download(_: User = Depends(require_admin)):
    with backup.lock:
        path = backup.make_backup()
    backup.record_backup("download", path.name, path.stat().st_size)
    return FileResponse(
        path, media_type="application/gzip", filename=path.name,
        background=BackgroundTask(shutil.rmtree, path.parent, ignore_errors=True),
    )


@router.get("/saved/{name}")
def download_saved(name: str, _: User = Depends(require_admin)):
    if not backup.NAME_RE.match(name):
        raise HTTPException(404, "No such backup")
    path = backup.backups_dir() / name
    if not path.is_file():
        raise HTTPException(404, "No such backup")
    return FileResponse(path, media_type="application/gzip", filename=name)


@router.post("/inspect")
def inspect_upload(file: UploadFile = File(...), _: User = Depends(require_admin)):
    """Store an uploaded backup and describe it; nothing is imported yet."""
    up = _uploads_dir()
    for old in up.iterdir():  # forget uploads that were never confirmed
        if time.time() - old.stat().st_mtime > _UPLOAD_MAX_AGE:
            old.unlink(missing_ok=True)
    token = secrets.token_hex(16)
    path = up / f"{_UPLOAD_PREFIX}{token}.json.gz"
    with path.open("wb") as out:
        shutil.copyfileobj(file.file, out, 1024 * 1024)
    try:
        summary = backup.inspect(path)
    except backup.BackupError as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(400, str(exc)) from None
    return {"token": token, "file": file.filename, "size": path.stat().st_size, **summary}


class ImportRequest(BaseModel):
    token: str = Field(pattern=r"^[0-9a-f]{32}$")
    confirm: bool = False


@router.post("/import")
def import_backup(req: ImportRequest, response: Response, user: User = Depends(require_admin),
                  db: Session = Depends(get_db)):
    if not req.confirm:
        raise HTTPException(400, "Confirm that the current data will be replaced.")
    path = _uploads_dir() / f"{_UPLOAD_PREFIX}{req.token}.json.gz"
    if not path.is_file():
        raise HTTPException(404, "The uploaded file is gone (uploads are kept for an hour). Choose it again.")

    from ..poller import listener, poller  # local import: the poller imports the models

    # this request's own session read the signed-in user; end its transaction,
    # otherwise its lock on the users table blocks the restore
    username = user.username
    db.close()

    with backup.lock:
        # the current data first, so a wrong import can be undone
        safety = backup.make_backup(backup.backups_dir(), suffix="before-import")
        backup.prune(backup.backups_dir(), backup.KEEP_SAFETY)
        running = settings.poll_enabled
        if running:
            poller.stop()
            listener.stop()
        try:
            restored = backup.restore(path)
        except backup.BackupError as exc:
            raise HTTPException(400, str(exc)) from None
        except Exception as exc:  # noqa: BLE001
            log.exception("import failed")
            raise HTTPException(500, f"Import failed, the current data was not changed: {exc}") from None
        finally:
            if running:
                poller._next_due.clear()  # device ids may now belong to other cameras
                poller.start()
                listener.start()
            path.unlink(missing_ok=True)
    log.warning("backup imported by %s: %s", username, restored)
    backup.update_state(last_import={
        "at": backup.utcnow().isoformat(), "by": username, "rows": restored, "safety": safety.name,
    })
    # the accounts now come from the backup: sign in again with one of them
    response.delete_cookie(SESSION_COOKIE)
    return {"imported": restored, "safety_backup": safety.name, "signed_out": True}


@router.delete("/upload/{token}")
def cancel_upload(token: str, _: User = Depends(require_admin)):
    if len(token) == 32 and all(c in "0123456789abcdef" for c in token):
        (_uploads_dir() / f"{_UPLOAD_PREFIX}{token}.json.gz").unlink(missing_ok=True)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# FTP
# --------------------------------------------------------------------------- #


class FtpSettings(BaseModel):
    enabled: bool = False
    host: str = ""
    port: int = Field(default=21, ge=1, le=65535)
    user: str = ""
    # blank = keep the password already saved
    password: Optional[str] = None
    folder: str = "/"
    tls: bool = False
    schedule: Literal["daily", "interval"] = "daily"
    daily_time: str = Field(default="02:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    interval_hours: int = Field(default=24, ge=1, le=24 * 30)
    keep: int = Field(default=14, ge=1, le=1000)
    timezone: str = "UTC"


def _merged(cfg: FtpSettings) -> dict:
    data = cfg.model_dump()
    data["host"] = data["host"].strip()
    if not data.get("password"):
        data["password"] = backup_ftp.load().get("password") or ""
    return data


@router.put("/ftp")
def save_ftp(cfg: FtpSettings, _: User = Depends(require_admin)):
    data = _merged(cfg)
    if data["enabled"] and not data["host"]:
        raise HTTPException(400, "Enter the FTP server before switching automatic backups on.")
    old = backup_ftp.load()
    schedule_keys = ("enabled", "schedule", "daily_time", "interval_hours", "timezone", "host")
    data["saved_at"] = (backup.utcnow().isoformat() if any(old.get(k) != data.get(k) for k in schedule_keys)
                        else old.get("saved_at"))
    return {"ftp": _ftp_public(backup_ftp.save(data))}


@router.post("/ftp/test")
def test_ftp(cfg: FtpSettings, _: User = Depends(require_admin)):
    data = _merged(cfg)
    if not data["host"]:
        raise HTTPException(400, "Enter the FTP server first.")
    ok, msg = backup_ftp.test(data)
    return {"ok": ok, "message": msg}


@router.post("/ftp/run")
def run_ftp(_: User = Depends(require_admin)):
    cfg = backup_ftp.load()
    if not cfg.get("host"):
        raise HTTPException(400, "Save the FTP settings first.")
    return backup_ftp.run(cfg, trigger="manual")
