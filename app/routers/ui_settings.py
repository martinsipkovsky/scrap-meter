"""The Settings tab: each user's look and behaviour of the pages, and the
defaults for everyone (administrators). See app.ui_settings."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import ui_settings
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
