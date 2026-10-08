"""Jobs and their ideal cycle times (the OEE performance factor).

A job is a name the stations count under (CounterState.job_name,
Reading.job_name): the job value a device supplies, a station's default job,
or the job of a manual entry. Every job a station has counted is listed on
the Jobs tab, where jobs can also be added before production starts. Its
cycle time is set as X seconds per shot (one machine cycle) making Y pieces
(cavities); the ideal time per piece, X / Y, is what OEE performance uses
(Job.ideal_cycle_s). Rows of counted jobs are added by ``sync`` when the list
is read and on startup, never while a reading is recorded, so two writers
can't collide there.

Up to 1.6 the cycle time was set per station; ``upgrade`` copies it to the
jobs each station has run, once per database.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import func, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import pieces
from .models import CounterState, Job, Meta, SourceState, Station, utcnow

log = logging.getLogger("cognex.jobs")

UPGRADE_KEY = "jobs_v1"
MAX_PIECES_PER_SHOT = 1000


def cycle_times(db: Session) -> dict[str, float]:
    """{job name: ideal seconds per piece} for the jobs that have one."""
    return {n: s for n, s in db.query(Job.name, Job.ideal_cycle_s).filter(Job.ideal_cycle_s.isnot(None))}


def sync(db: Session) -> int:
    """Add a row for every counted job that has none. Returns how many."""
    known = {n for (n,) in db.query(Job.name)}
    names = {n for (n,) in db.query(CounterState.job_name).distinct() if n} - known
    if not names:
        return 0
    db.add_all(Job(name=n) for n in sorted(names))
    try:
        db.commit()
    except IntegrityError:  # another request added them at the same moment
        db.rollback()
        return 0
    return len(names)


def listing(db: Session) -> list[dict]:
    """Every job with its cycle time, the stations that counted it and when
    it was last counted; the jobs being run now first."""
    sync(db)
    station_names = {i: n for i, n in db.query(Station.id, Station.name)}
    from . import stations

    current: dict[str, list[str]] = {}
    for st in db.query(Station).order_by(Station.sort_order, Station.name):
        for job in stations.current_jobs(db, st):
            current.setdefault(job, []).append(st.name)
    ran: dict[str, list[str]] = {}
    last: dict[str, dt.datetime] = {}
    for sid, job, updated in db.query(CounterState.station_id, CounterState.job_name, CounterState.updated_at):
        if sid in station_names:
            ran.setdefault(job, []).append(station_names[sid])
        if updated and (job not in last or aware(updated) > last[job]):
            last[job] = aware(updated)
    # pictures of pieces not judged yet, per job (piece rules)
    open_pieces: dict[str, list[dict]] = {}
    for sid, src_id, job, piece in db.query(SourceState.station_id, SourceState.source_id, SourceState.job,
                                             SourceState.open_piece):
        if piece and (piece.get("ok") or piece.get("nok")) and sid in station_names:
            open_pieces.setdefault(job, []).append({"station": station_names[sid], **piece})
    out = [{
        "id": j.id,
        "name": j.name,
        "ideal_cycle_s": j.ideal_cycle_s,
        # a time per piece set another way (Raw data tab) reads as 1 piece per shot
        "shot_s": j.shot_s if j.shot_s is not None else j.ideal_cycle_s,
        "pieces_per_shot": (j.pieces_per_shot or 1) if j.shot_s is not None else 1,
        "piece_rule": j.piece_rule,
        "piece_rule_text": pieces.describe(j.piece_rule),
        "open_pieces": open_pieces.get(j.name, []),
        "running_on": current.get(j.name, []),
        "stations": sorted(set(ran.get(j.name, []))),
        "last_counted_at": last.get(j.name),
        "updated_at": j.updated_at,
    } for j in db.query(Job).all()]
    out.sort(key=lambda r: (not r["running_on"], r["name"].lower()))
    return out


def aware(t: dt.datetime) -> dt.datetime:
    return t.replace(tzinfo=dt.timezone.utc) if t.tzinfo is None else t


def set_piece_rule(db: Session, name: str, rule: dict | None) -> Job:
    job = db.query(Job).filter(Job.name == name).first()
    if job is None:
        job = Job(name=name)
        db.add(job)
    job.piece_rule = rule
    return job


def apply_cycle(job: Job, shot_s: float | None, pieces_per_shot: int | None = 1) -> None:
    """Set X seconds per shot making Y pieces (None clears the cycle time;
    the pieces per shot are kept then)."""
    y = int(pieces_per_shot or 1)
    if not 1 <= y <= MAX_PIECES_PER_SHOT:
        raise ValueError(f"pieces per shot must be 1 - {MAX_PIECES_PER_SHOT}")
    job.pieces_per_shot = y
    job.shot_s = shot_s
    job.ideal_cycle_s = round(shot_s / y, 6) if shot_s else None


def set_cycle(db: Session, name: str, shot_s: float | None, pieces_per_shot: int | None = 1) -> Job:
    """``shot_s`` seconds per shot of ``pieces_per_shot`` pieces; a bare
    per-piece time (1.14 and older) is ``shot_s`` with 1 piece per shot."""
    job = db.query(Job).filter(Job.name == name).first()
    if job is None:
        job = Job(name=name)
        db.add(job)
    apply_cycle(job, shot_s, pieces_per_shot)
    return job


def fill_shots(engine: Engine) -> None:
    """Jobs from 1.14 or older (or restored from such a backup) have only a
    time per piece: it becomes X seconds per shot of 1 piece."""
    with engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET shot_s = ideal_cycle_s, pieces_per_shot = 1 "
                          "WHERE shot_s IS NULL AND ideal_cycle_s IS NOT NULL"))
        conn.execute(text("UPDATE jobs SET pieces_per_shot = 1 WHERE pieces_per_shot IS NULL"))


def copy_station_cycles(db: Session, picks: list[tuple[dt.datetime, str, str, float]]) -> list[str]:
    """Give jobs without a cycle time the one of a station that ran them.

    ``picks`` holds (when the station last counted the job, station, job,
    seconds). When stations disagree on a job, the most recent wins. Returns
    a line per job whose stations disagreed.
    """
    by_job: dict[str, list[tuple[dt.datetime, str, float]]] = {}
    for when, station, job, seconds in picks:
        by_job.setdefault(job, []).append((when, station, seconds))
    have = set(cycle_times(db))
    conflicts = []
    for job, rows in sorted(by_job.items()):
        if job in have:
            continue
        rows.sort(key=lambda r: r[0])
        when, station, seconds = rows[-1]
        set_cycle(db, job, seconds)
        if len({r[2] for r in rows}) > 1:
            others = ", ".join(f"{s} {v:g} s" for _, s, v in rows[:-1] if v != seconds)
            conflicts.append(f"job '{job}': kept {seconds:g} s from station '{station}' (ran it last); "
                             f"also set: {others}")
    return conflicts


def upgrade(engine: Engine) -> int:
    """Copy the per-station cycle times of a 1.6 (or older) database to the
    jobs each station has run. Runs once per database (recorded in the meta
    table; restoring an older backup runs it again on that data). Returns the
    number of jobs given a cycle time."""
    fill_shots(engine)
    with engine.connect() as conn:
        if conn.execute(text("SELECT 1 FROM meta WHERE key = :k"), {"k": UPGRADE_KEY}).first():
            with Session(bind=engine) as db:
                sync(db)
            return 0
    with Session(bind=engine) as db:
        sync(db)
        epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
        picks = []
        for st in db.query(Station).filter(Station.ideal_cycle_s.isnot(None)).all():
            rows = (db.query(CounterState.job_name, func.max(CounterState.updated_at))
                    .filter(CounterState.station_id == st.id).group_by(CounterState.job_name).all())
            for job, updated in rows:
                picks.append((aware(updated) if updated else epoch, st.name, job, st.ideal_cycle_s))
        before = len(cycle_times(db))
        conflicts = copy_station_cycles(db, picks)
        db.flush()
        made = len(cycle_times(db)) - before
        db.merge(Meta(key=UPGRADE_KEY, value=utcnow().isoformat()))
        db.commit()
    if made:
        log.info("upgrade: copied the stations' ideal cycle times to %d job(s)", made)
    for line in conflicts:
        log.warning("upgrade: stations had different cycle times for %s", line)
    return made
