"""Map tab API: the network map's nodes and links, and its saved layout."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import netmap
from ..database import get_db
from ..dependencies import require_permission
from ..models import User

router = APIRouter(prefix="/api/map", tags=["map"])


@router.get("")
def graph(db: Session = Depends(get_db), _: User = Depends(require_permission("view_dashboard"))):
    return netmap.graph(db)


@router.put("/layout")
def save_layout(body: dict, _: User = Depends(require_permission("manage_devices"))):
    """{"positions": {"server" | "d<id>" | "s<id>": [x, y] or null}}; null
    puts a node back in the automatic layout."""
    positions = body.get("positions")
    if not isinstance(positions, dict):
        raise HTTPException(400, "positions must be an object")
    return netmap.save_layout(positions)


@router.delete("/layout")
def reset_layout(_: User = Depends(require_permission("manage_devices"))):
    netmap.settings_store.save(netmap.KEY, None)
    return {}
