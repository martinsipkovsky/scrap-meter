"""The Chat room tab (app.chatroom): the room, its messages, sending, and the
administrator's choice of the room."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import chatroom
from ..database import get_db
from ..dependencies import require_api_user, require_permission
from ..models import NotificationProvider, User

router = APIRouter(prefix="/api/chat", tags=["chat room"])

_perm = require_permission("chat_room")


def _admin(user: User = Depends(require_api_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Only administrators can choose the chat room")
    return user


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=chatroom.MAX_TEXT)


class RoomIn(BaseModel):
    kind: str
    chat: str = Field(min_length=1, max_length=120)
    name: str = Field(default="", max_length=200)
    provider_id: Optional[int] = None


@router.get("/room")
def get_room(db: Session = Depends(get_db), user: User = Depends(_perm)):
    return {**chatroom.status(db), "can_choose": user.is_admin}


@router.get("/options")
def room_options(db: Session = Depends(get_db), _: User = Depends(_admin)):
    return chatroom.options(db)


@router.put("/room")
def set_room(payload: Optional[RoomIn] = None, db: Session = Depends(get_db), user: User = Depends(_admin)):
    """Choose the room; no body (or null) clears it."""
    if payload is None:
        chatroom.set_room(None)
        return get_room(db, user)
    if payload.kind not in chatroom.KINDS:
        raise HTTPException(400, f"kind must be one of {sorted(chatroom.KINDS)}")
    with_provider = payload.kind in ("telegram", "discord")  # the bot that reads and writes the chat
    if with_provider:
        p = db.get(NotificationProvider, payload.provider_id) if payload.provider_id is not None else None
        if p is None or p.kind != payload.kind:
            label = chatroom.KINDS[payload.kind]
            raise HTTPException(400, f"A {label} room needs the id of a {label} provider")
    room = {"kind": payload.kind, "chat": payload.chat.strip(), "name": payload.name.strip() or payload.chat.strip(),
            "provider_id": payload.provider_id if with_provider else None}
    chatroom.set_room(room)
    return get_room(db, user)


@router.get("/messages")
def list_messages(after: Optional[int] = None, before: Optional[int] = None, limit: int = 100,
                  db: Session = Depends(get_db), _: User = Depends(_perm)):
    return chatroom.messages(db, after=after, before=before, limit=max(1, min(limit, 500)))


@router.post("/messages", status_code=201)
def send_message(payload: MessageIn, db: Session = Depends(get_db), user: User = Depends(_perm)):
    try:
        return chatroom.send(db, user, payload.text)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
