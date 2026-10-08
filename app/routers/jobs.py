"""The Jobs tab: every job, its cycle time (X s per shot of Y pieces, the OEE
performance) and piece rule; jobs can be added before production. See
app.jobs."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import jobs, pieces
from ..database import get_db
from ..dependencies import require_permission
from ..models import CounterState, Job, User

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class JobUpdate(BaseModel):
    # seconds per shot (one machine cycle); None clears the cycle time
    shot_s: Optional[float] = Field(default=None, gt=0, le=86400)
    # pieces one shot makes (cavities)
    pieces_per_shot: Optional[int] = Field(default=None, ge=1, le=jobs.MAX_PIECES_PER_SHOT)
    # up to 1.14: ideal seconds per piece (= shot_s with 1 piece per shot)
    ideal_cycle_s: Optional[float] = Field(default=None, gt=0, le=86400)
    # how camera pictures make pieces (app.pieces); None = 1 picture, 1 piece
    piece_rule: Optional[dict] = None


class JobCreate(JobUpdate):
    # the job name or number the device reports, so it matches when production starts
    name: str = Field(min_length=1, max_length=255)


def _out(job: Job) -> dict:
    return {"id": job.id, "name": job.name, "ideal_cycle_s": job.ideal_cycle_s, "shot_s": job.shot_s,
            "pieces_per_shot": job.pieces_per_shot or 1, "piece_rule": job.piece_rule,
            "piece_rule_text": pieces.describe(job.piece_rule)}


def _apply(job: Job, data: dict) -> None:
    try:
        if "shot_s" in data or "pieces_per_shot" in data:
            shot = data["shot_s"] if "shot_s" in data else job.shot_s
            n = data.get("pieces_per_shot") or job.pieces_per_shot or 1
            jobs.apply_cycle(job, shot, n)
        elif "ideal_cycle_s" in data:
            jobs.apply_cycle(job, data["ideal_cycle_s"], 1)
        if "piece_rule" in data:
            job.piece_rule = pieces.normalize(data["piece_rule"])
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("")
def list_jobs(db: Session = Depends(get_db), _: User = Depends(require_permission("view_dashboard"))):
    """Every job: the ones the stations have counted and the ones added here."""
    return jobs.listing(db)


@router.post("", status_code=201)
def create_job(
    payload: JobCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("manage_devices")),
):
    """Add a job before production, by the name the device will report."""
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "The job needs a name")
    if db.query(Job.id).filter(Job.name == name).first():
        raise HTTPException(409, f"Job '{name}' is already in the list")
    job = Job(name=name, pieces_per_shot=1)
    _apply(job, payload.model_dump(exclude_unset=True, exclude={"name"}))
    db.add(job)
    db.commit()
    return _out(job)


@router.patch("/{job_id}")
def update_job(
    job_id: int,
    payload: JobUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("manage_devices")),
):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    _apply(job, payload.model_dump(exclude_unset=True))
    db.commit()
    return _out(job)


@router.delete("/{job_id}", status_code=204)
def delete_job(
    job_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("manage_devices")),
):
    """Remove a job no station has counted (added here and not run yet, or
    its stations were deleted); a counted job would come back by itself."""
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if db.query(CounterState.id).filter(CounterState.job_name == job.name).first():
        raise HTTPException(409, "A station has counted this job, so it stays in the list")
    db.delete(job)
    db.commit()
