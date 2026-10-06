"""The jobs list and their ideal cycle times (OEE performance); see app.jobs."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import jobs
from ..database import get_db
from ..dependencies import require_permission
from ..models import CounterState, Job, User

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class JobUpdate(BaseModel):
    # ideal seconds per piece; None clears it
    ideal_cycle_s: Optional[float] = Field(default=None, gt=0, le=86400)


@router.get("")
def list_jobs(db: Session = Depends(get_db), _: User = Depends(require_permission("view_dashboard"))):
    """Every job the stations have counted, with its cycle time."""
    return jobs.listing(db)


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
    job.ideal_cycle_s = payload.ideal_cycle_s
    db.commit()
    return {"id": job.id, "name": job.name, "ideal_cycle_s": job.ideal_cycle_s}


@router.delete("/{job_id}", status_code=204)
def delete_job(
    job_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("manage_devices")),
):
    """Remove a job no station has counted any more (its stations were
    deleted); a counted job would come back by itself."""
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if db.query(CounterState.id).filter(CounterState.job_name == job.name).first():
        raise HTTPException(409, "A station has counted this job, so it stays in the list")
    db.delete(job)
    db.commit()
