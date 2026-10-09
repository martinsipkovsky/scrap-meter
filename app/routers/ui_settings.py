"""The Settings tab: each user's look and behaviour of the pages, the
defaults for everyone and the Developer options (administrators). See
app.ui_settings and app.dev_options."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import dev_options, ping, ui_settings
from ..config import settings
from ..notifiers.whatsapp_linked import link as whatsapp_link
from ..database import get_db
from ..dependencies import require_api_user
from ..models import User

router = APIRouter(prefix="/api/ui-settings", tags=["settings"])


@router.get("")
def get_settings(user: User = Depends(require_api_user)):
    return {
        "choices": ui_settings.choices(),
        "builtin": ui_settings.builtin(),
        "defaults": ui_settings.defaults(),
        "admin_defaults": ui_settings.stored_defaults(),
        "mine": ui_settings.clean(user.ui_settings),
        "effective": ui_settings.effective(user),
        "can_set_defaults": user.is_admin,
        "dev_options": dev_options.load() if user.is_admin else None,
        "dev_labels": dev_options.OPTIONS if user.is_admin else None,
        "ping": ping.load() if user.is_admin else None,
    }


@router.put("/mine")
def save_mine(payload: dict, db: Session = Depends(get_db), user: User = Depends(require_api_user)):
    """The user's own choices; a key left out (or null) follows the default."""
    user.ui_settings = ui_settings.clean(payload) or None
    db.commit()
    return {"mine": ui_settings.clean(user.ui_settings), "effective": ui_settings.effective(user)}


@router.put("/defaults")
def save_defaults(payload: dict, user: User = Depends(require_api_user)):
    """Defaults for everyone; a key left out follows the built-in default."""
    if not user.is_admin:
        raise HTTPException(403, "Only an administrator can set the defaults for everyone")
    return {"admin_defaults": ui_settings.save_defaults(payload), "defaults": ui_settings.defaults()}


@router.put("/ping")
def save_ping(payload: dict, user: User = Depends(require_api_user)):
    """Ping devices (administrators): on / off and how often."""
    if not user.is_admin:
        raise HTTPException(403, "Only an administrator can change the ping setting")
    if "enabled" in payload and not isinstance(payload["enabled"], bool):
        raise HTTPException(400, "enabled must be true or false")
    return ping.save(payload)


@router.put("/dev-options")
def save_dev_options(payload: dict, user: User = Depends(require_api_user)):
    """Developer options (administrators). Turning the WhatsApp virtual client
    off stops it (the login is kept); on starts it again."""
    if not user.is_admin:
        raise HTTPException(403, "Only an administrator can change the developer options")
    before = dev_options.load()
    after = dev_options.save({k: v for k, v in payload.items() if k in dev_options.OPTIONS})
    if before["whatsapp_linked"] and not after["whatsapp_linked"]:
        whatsapp_link.stop()
        whatsapp_link.state, whatsapp_link.qr, whatsapp_link.error = "unknown", None, None
    elif after["whatsapp_linked"] and not before["whatsapp_linked"] and settings.whatsapp_enabled:
        whatsapp_link.start()  # reconnects a phone linked earlier; exits at once if none
    return after
