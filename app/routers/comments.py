"""Station comments: anyone who sees the dashboard can write and read them,
only an admin can delete one. See app.comments."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import comments
from ..database import get_db
from ..dependencies import require_api_user, require_permission
from ..models import Station, StationComment, User

router = APIRouter(tags=["comments"])


class CommentIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


@router.get("/api/stations/{station_id}/comments")
def list_comments(
    station_id: int,
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("view_dashboard")),
):
    """The station's comments, newest first."""
    rows = (db.query(StationComment).filter(StationComment.station_id == station_id)
            .order_by(StationComment.created_at.desc(), StationComment.id.desc()).limit(limit).all())
    return [comments.out(c) for c in rows]


@router.post("/api/stations/{station_id}/comments", status_code=201)
def add_comment(
    station_id: int,
    payload: CommentIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("view_dashboard")),
):
    st = db.get(Station, station_id)
    if st is None:
        raise HTTPException(404, "Station not found")
    body = payload.text.strip()
    if not body:
        raise HTTPException(400, "Write a comment first")
    return comments.out(comments.add(db, st, body, user.username))


@router.delete("/api/comments/{comment_id}", status_code=204)
def delete_comment(comment_id: int, db: Session = Depends(get_db), user: User = Depends(require_api_user)):
    if not user.is_admin:
        raise HTTPException(403, "Only an administrator can delete comments")
    c = db.get(StationComment, comment_id)
    if c is None:
        raise HTTPException(404, "Comment not found")
    db.delete(c)
    db.commit()
