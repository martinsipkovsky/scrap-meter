"""Chat commands (see app.commands): the prefix, the commands, the chats they
can answer in, a preview of a reply, and the log of handled commands."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import commands
from ..notifiers import discord, signal
from ..notifiers.base import NotifierError
from ..notifiers.whatsapp_linked import link as whatsapp_link
from ..database import get_db
from ..dependencies import require_permission
from ..models import ChatCommand, CommandLog, NotificationProvider, User

router = APIRouter(prefix="/api/notifications/commands", tags=["commands"])

_perm = require_permission("manage_notifications")
_KEYWORD = r"^[a-z0-9_-]{1,40}$"


class CommandIn(BaseModel):
    keyword: str = Field(pattern=_KEYWORD)
    description: str = Field(default="", max_length=200)
    period: str = "dashboard"
    hours: int = Field(default=8, ge=1, le=24 * 31)
    timezone: str = "UTC"
    header: str = ""
    line: str = ""
    footer: str = ""
    # only stations in production in the last N days; None = every station
    active_days: Optional[int] = Field(default=None, ge=1, le=3650)
    group_ids: list[str] = []
    enabled: bool = True


class CommandPatch(BaseModel):
    keyword: Optional[str] = Field(default=None, pattern=_KEYWORD)
    description: Optional[str] = Field(default=None, max_length=200)
    period: Optional[str] = None
    hours: Optional[int] = Field(default=None, ge=1, le=24 * 31)
    timezone: Optional[str] = None
    header: Optional[str] = None
    line: Optional[str] = None
    footer: Optional[str] = None
    active_days: Optional[int] = Field(default=None, ge=1, le=3650)  # null clears it
    group_ids: Optional[list[str]] = None
    enabled: Optional[bool] = None


class PrefixIn(BaseModel):
    prefix: str


class PreviewIn(CommandIn):
    keyword: str = "preview"
    camera: str = ""


def _check(data: dict, db: Session, current_id: int | None = None) -> None:
    if data.get("period") is not None and data["period"] not in commands.PERIODS:
        raise HTTPException(400, f"period must be one of {sorted(commands.PERIODS)}")
    if data.get("keyword"):
        clash = db.query(ChatCommand).filter(ChatCommand.keyword == data["keyword"]).first()
        if clash and clash.id != current_id:
            raise HTTPException(409, f"A command '{data['keyword']}' already exists")


@router.get("")
def overview(db: Session = Depends(get_db), _: User = Depends(_perm)):
    return {
        "prefix": commands.get_prefix(),
        "commands": db.query(ChatCommand).order_by(ChatCommand.keyword).all(),
        "periods": commands.PERIODS,
        "placeholders": commands.PLACEHOLDERS,
    }


@router.get("/chats")
def chats(db: Session = Depends(get_db), _: User = Depends(_perm)):
    """The groups and channels a command can answer in: WhatsApp groups of
    the linked phone, channels of the Discord bots, Signal groups."""
    found, problems = [], []
    if whatsapp_link.state == "connected":
        try:
            found += [{"id": g["id"], "name": g["name"], "messenger": "WhatsApp"} for g in whatsapp_link.groups()]
        except NotifierError as exc:
            problems.append(f"WhatsApp: {exc}")
    for p in db.query(NotificationProvider).filter(NotificationProvider.kind == "discord").order_by(NotificationProvider.id):
        cfg = p.config or {}
        if discord.mode(cfg) != "bot":
            continue
        for ch in discord.channel_list(cfg.get("channel_ids") or cfg.get("channel_id")):
            name = ch
            try:
                name = discord.channel_name(cfg, ch)
            except NotifierError as exc:
                problems.append(f"Discord '{p.name}', channel {ch}: {exc}")
            found.append({"id": ch, "name": name, "messenger": f"Discord ({p.name})"})
    if signal.base_url():
        try:
            if signal.number():
                found += [{"id": g["id"], "name": g["name"], "messenger": "Signal"} for g in signal.groups()]
        except NotifierError as exc:
            problems.append(f"Signal: {exc}")
    return {"chats": found, "problems": problems}


@router.put("/prefix")
def set_prefix(payload: PrefixIn, _: User = Depends(_perm)):
    prefix = payload.prefix.strip()
    if not commands.valid_prefix(prefix):
        raise HTTPException(400, "The prefix must be 1-3 characters without spaces, starting with a symbol such as ! / # or .")
    commands.set_prefix(prefix)
    return {"prefix": prefix}


@router.post("", status_code=201)
def create(payload: CommandIn, db: Session = Depends(get_db), _: User = Depends(_perm)):
    data = payload.model_dump()
    _check(data, db)
    cmd = ChatCommand(**data)
    db.add(cmd)
    db.commit()
    db.refresh(cmd)
    return cmd


@router.patch("/{command_id}")
def update(command_id: int, payload: CommandPatch, db: Session = Depends(get_db), _: User = Depends(_perm)):
    cmd = db.get(ChatCommand, command_id)
    if not cmd:
        raise HTTPException(404, "Command not found")
    data = payload.model_dump(exclude_unset=True)
    _check(data, db, cmd.id)
    for key, value in data.items():
        setattr(cmd, key, value)
    db.commit()
    db.refresh(cmd)
    return cmd


@router.delete("/{command_id}", status_code=204)
def delete(command_id: int, db: Session = Depends(get_db), _: User = Depends(_perm)):
    cmd = db.get(ChatCommand, command_id)
    if not cmd:
        raise HTTPException(404, "Command not found")
    db.delete(cmd)
    db.commit()


@router.post("/preview")
def preview(payload: PreviewIn, db: Session = Depends(get_db), _: User = Depends(_perm)):
    """The reply the command would send right now (nothing is sent)."""
    data = payload.model_dump(exclude={"camera", "group_ids", "enabled"})
    _check({"period": data["period"]}, db)
    return {"reply": commands.render(db, ChatCommand(**data), payload.camera)}


@router.get("/log")
def command_log(limit: int = 50, db: Session = Depends(get_db), _: User = Depends(_perm)):
    rows = db.query(CommandLog).order_by(CommandLog.created_at.desc()).limit(min(limit, 500)).all()
    return [
        {"id": r.id, "chat": r.chat, "chat_name": r.chat_name, "sender": r.sender, "text": r.text,
         "keyword": r.keyword, "outcome": r.outcome, "reply": r.reply, "created_at": r.created_at}
        for r in rows
    ]
