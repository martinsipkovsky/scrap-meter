"""Recalculate counts from the raw readings (Database tab, admins only).

Up to 1.25 a drop of any of a source's counters restarted all of them
(app.counters.deltas_1_25), so a device whose OK counter was reset on its own
had its whole NOK counter counted again at every such reset. Every read is
logged with the raw counters, so those readings can be found and put right:

* ``plan`` walks a station's device readings in the order they were logged
  and works out, for each one, the pieces between it and the source's
  previous reading by the old rule and by the current one
  (app.counters.deltas). Where they differ, the reading is corrected only
  when what it counted is exactly what the old rule gave: the plain case
  (the station was in production), or a start of production that counted
  the pieces held for the start window (the old sums must match too).
  Anything else (a job with a piece rule, a source edited meanwhile) is left
  as it is and reported as unclear. Readings from before 1.8 hold no counted
  pieces, only running totals; there the difference to the job's previous
  reading must match.
* ``preview`` adds the plans up per station and day; it changes nothing.
* ``apply`` writes them: the reading's counted pieces (ok_added / nok_added),
  the running totals of that and every later reading of the job, the job's
  counters (CounterState, with the baseline of a dashboard reset made after
  the reading), and the daily data. A station is corrected under its lock,
  so reads that arrive meanwhile wait and then carry on from the corrected
  totals. The caller takes a backup first (app.routers.recount).

Corrected readings then match the current rule, so running it again finds
nothing more to do.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from . import daily, production, stations
from .counters import deltas, deltas_1_25
from .models import CounterState, Reading, Station
from .scrap_stats import day_bounds, station_parts

log = logging.getLogger("cognex.recount")

# unclear readings listed in the preview (all of them are counted)
MAX_UNCLEAR = 50

_COLS = (Reading.id, Reading.source_id, Reading.job_name, Reading.created_at, Reading.raw_pass, Reading.raw_fail,
         Reading.raw_count, Reading.total_pass, Reading.total_fail, Reading.ok_added, Reading.nok_added,
         Reading.in_production)


def _aware(t: dt.datetime) -> dt.datetime:
    return t.replace(tzinfo=dt.timezone.utc) if t.tzinfo is None else t


def _sub(a, b) -> tuple[int, int, int]:
    return tuple(x - y for x, y in zip(a, b))  # type: ignore[return-value]


def _add(a, b) -> tuple[int, int, int]:
    return tuple(x + y for x, y in zip(a, b))  # type: ignore[return-value]


@dataclass
class Fix:
    """One reading that counted too much: ``excess`` (OK, NOK, total) goes."""

    id: int
    job: str
    t: dt.datetime
    counted: tuple[int, int]
    corrected: tuple[int, int]
    excess: tuple[int, int, int]
    legacy: bool = False


@dataclass
class Plan:
    station: Station
    fixes: list[Fix] = field(default_factory=list)
    # readings the rules disagree on, but that did not count what the old rule gave
    unclear: list[dict] = field(default_factory=list)
    unclear_count: int = 0
    # per job: what its counters lose, and the part of it from before the
    # job's last dashboard reset (its baseline loses that)
    per_job: dict[str, list[int]] = field(default_factory=dict)
    before_reset: dict[str, list[int]] = field(default_factory=dict)
    readings: int = 0


@dataclass
class _Source:
    """A source's readings as the walk has seen them."""

    prev_raw: tuple[int, int, int] | None = None  # before the reads of ``t``
    raw: tuple[int, int, int] | None = None
    t: dt.datetime | None = None
    # reads that counted nothing (not in production): [time, old, new]
    held: list = field(default_factory=list)
    last_unclear: int | None = None


def plan(db: Session, station: Station) -> Plan:
    out = Plan(station)
    srcs = {s["id"]: s for s in station.source_list()}
    windows = sorted({int(s["start_window_s"]) for s in srcs.values()} | {production.DEFAULT_START_WINDOW_S})
    resets = {cs.job_name: _aware(cs.reset_at)
              for cs in db.query(CounterState).filter(CounterState.station_id == station.id) if cs.reset_at}
    by_source: dict[str, _Source] = {}
    legacy: dict[str, tuple] = {}  # job -> (raw, total_pass, total_fail) of its previous reading

    def unclear(r, t, counted, old, new) -> None:
        out.unclear_count += 1
        if len(out.unclear) < MAX_UNCLEAR:
            out.unclear.append({"id": r.id, "at": t, "job": r.job_name, "raw_ok": r.raw_pass, "raw_nok": r.raw_fail,
                                "counted_ok": counted[0], "counted_nok": counted[1],
                                "old_ok": old[0], "old_nok": old[1], "new_ok": new[0], "new_nok": new[1]})

    def fix(r, t, counted, corrected, excess, is_legacy=False) -> None:
        out.fixes.append(Fix(r.id, r.job_name, t, counted, corrected, excess, is_legacy))
        total = out.per_job.setdefault(r.job_name, [0, 0, 0])
        early = out.before_reset.setdefault(r.job_name, [0, 0, 0])
        reset_at = resets.get(r.job_name)
        for i in range(3):
            total[i] += excess[i]
            if reset_at is not None and t <= reset_at:
                early[i] += excess[i]

    rows = db.execute(select(*_COLS).where(Reading.station_id == station.id, Reading.manual.isnot(True))
                      .order_by(Reading.id).execution_options(yield_per=5000))
    for r in rows:
        out.readings += 1
        if not r.job_name:
            continue
        t = _aware(r.created_at)
        if r.ok_added is None or r.source_id is None:  # logged before 1.8
            raw = (r.raw_pass or 0, r.raw_fail or 0, r.raw_count or 0)
            before = legacy.get(r.job_name)
            legacy[r.job_name] = (raw, r.total_pass or 0, r.total_fail or 0)
            if before is None:
                continue
            old, new = deltas_1_25(before[0], raw), deltas(before[0], raw)
            if old == new:
                continue
            counted = ((r.total_pass or 0) - before[1], (r.total_fail or 0) - before[2])
            if counted == new[:2]:
                continue
            if counted == old[:2]:
                fix(r, t, counted, new[:2], _sub(old, new), is_legacy=True)
            else:
                unclear(r, t, counted, old, new)
            continue

        src = by_source.setdefault(r.source_id, _Source())
        same_read = src.t is not None and r.created_at == src.t
        if not same_read:
            src.prev_raw, src.t = src.raw, r.created_at
        elif src.last_unclear is not None and out.unclear and out.unclear[-1]["id"] == src.last_unclear:
            # two readings of one read (the last piece of the previous job, then
            # the read itself): only the second one is judged
            out.unclear.pop()
            out.unclear_count -= 1
        src.last_unclear = None
        has_count = bool((srcs.get(r.source_id) or {}).get("count"))
        raw = (r.raw_pass or 0, r.raw_fail or 0, (r.raw_count or 0) if has_count else 0)
        base, src.raw = src.prev_raw, raw
        if base is None:
            continue  # the source's first reading: its baseline
        old, new = deltas_1_25(base, raw), deltas(base, raw)
        if not has_count:
            old, new = (old[0], old[1], old[0] + old[1]), (new[0], new[1], new[0] + new[1])
        counted = (r.ok_added, r.nok_added or 0)
        ts = t.timestamp()
        if counted == (0, 0) and not r.in_production:
            # nothing counted: held for the start window
            src.held = [h for h in src.held if ts - h[0] <= windows[-1]]
            if any(old) or any(new):
                src.held.append([ts, old, new])
            continue
        held, src.held = src.held, []
        # what the read may have counted: its own pieces, or those and the
        # pieces held within a start window
        options = [(old, new)]
        for w in windows:
            h_old = h_new = (0, 0, 0)
            for at, o, n in held:
                if ts - at <= w:
                    h_old, h_new = _add(h_old, o), _add(h_new, n)
            options.append((_add(old, h_old), _add(new, h_new)))
        if all(o == n for o, n in options):
            continue
        if any(n[:2] == counted for _, n in options):
            continue  # counted by the current rule, or corrected before
        for o, n in options:
            if o != n and o[:2] == counted:
                fix(r, t, counted, n[:2], _sub(o, n))
                break
        else:
            unclear(r, t, counted, options[-1][0], options[-1][1])
            src.last_unclear = r.id
    return out


def _stations(db: Session) -> list[Station]:
    return db.query(Station).order_by(Station.sort_order, Station.name).all()


def _days(db: Session, p: Plan, tz: dt.tzinfo) -> list[dict]:
    """The plan per local day: what the day counts now and after."""
    by_id = {f.id: f for f in p.fixes}
    days: dict[dt.date, dict] = {}
    for f in p.fixes:
        d = days.setdefault(f.t.astimezone(tz).date(), {"readings": 0, "ok": 0, "nok": 0, "ok_after": 0, "nok_after": 0})
        d["readings"] += 1
    for day, d in days.items():
        start, end = day_bounds(day, day, tz)
        for r in station_parts(db, p.station, start, end):
            if r["excluded"] or not r["in_production"]:
                continue
            f = by_id.get(r["id"])
            d["ok"] += r["ok"]
            d["nok"] += r["nok"]
            d["ok_after"] += r["ok"] - (f.excess[0] if f else 0)
            d["nok_after"] += r["nok"] - (f.excess[1] if f else 0)
    return [{"day": day.isoformat(), **d} for day, d in sorted(days.items())]


def preview(db: Session, tz: dt.tzinfo) -> dict:
    """What ``apply`` would change, per station and day. Changes nothing."""
    rows, unclear = [], []
    total = {"readings": 0, "ok": 0, "nok": 0, "unclear": 0, "scanned": 0}
    for st in _stations(db):
        p = plan(db, st)
        total["scanned"] += p.readings
        total["unclear"] += p.unclear_count
        unclear += [{"station": st.name, **u} for u in p.unclear][:MAX_UNCLEAR - len(unclear)]
        if not p.fixes:
            continue
        ok, nok = sum(f.excess[0] for f in p.fixes), sum(f.excess[1] for f in p.fixes)
        total["readings"] += len(p.fixes)
        total["ok"] += ok
        total["nok"] += nok
        rows.append({"station_id": st.id, "station": st.name, "readings": len(p.fixes), "ok": ok, "nok": nok,
                     "first": p.fixes[0].t, "last": p.fixes[-1].t, "days": _days(db, p, tz)})
    return {"stations": rows, "total": total, "unclear": unclear}


_SHIFT = text("UPDATE readings SET total_pass = total_pass - :ok, total_fail = total_fail - :nok "
              "WHERE station_id = :sid AND job_name = :job AND manual IS NOT TRUE AND id >= :first AND id < :last")
_MAX_ID = 2 ** 62


def _write(db: Session, p: Plan) -> None:
    per_job: dict[str, list[Fix]] = {}
    for f in p.fixes:
        per_job.setdefault(f.job, []).append(f)
        if not f.legacy:
            db.execute(text("UPDATE readings SET ok_added = :ok, nok_added = :nok WHERE id = :id"),
                       {"ok": f.corrected[0], "nok": f.corrected[1], "id": f.id})
    for job, fixes in per_job.items():
        # the running totals from each corrected reading up to the next one
        ok = nok = 0
        for f, nxt in zip(fixes, [*fixes[1:], None]):
            ok, nok = ok + f.excess[0], nok + f.excess[1]
            if ok or nok:
                db.execute(_SHIFT, {"ok": ok, "nok": nok, "sid": p.station.id, "job": job, "first": f.id,
                                    "last": nxt.id if nxt else _MAX_ID})
        cs = (db.query(CounterState)
              .filter(CounterState.station_id == p.station.id, CounterState.job_name == job).first())
        if cs is None:
            continue
        gone, early = p.per_job[job], p.before_reset[job]
        cs.total_pass = max(cs.total_pass - gone[0], 0)
        cs.total_fail = max(cs.total_fail - gone[1], 0)
        cs.total_count = max(cs.total_count - gone[2], 0)
        cs.base_pass = max((cs.base_pass or 0) - early[0], 0)
        cs.base_fail = max((cs.base_fail or 0) - early[1], 0)
        cs.base_count = max((cs.base_count or 0) - early[2], 0)


def apply(db: Session) -> dict:
    """Correct every station (see the module doc). Returns what was done."""
    done = {"readings": 0, "ok": 0, "nok": 0, "stations": []}
    for st in _stations(db):
        with stations._lock(st.id):
            db.refresh(st)
            p = plan(db, st)
            if not p.fixes:
                db.rollback()
                continue
            try:
                _write(db, p)
                db.commit()
            except Exception:
                db.rollback()
                raise
        ok, nok = sum(f.excess[0] for f in p.fixes), sum(f.excess[1] for f in p.fixes)
        done["readings"] += len(p.fixes)
        done["ok"] += ok
        done["nok"] += nok
        done["stations"].append({"station": st.name, "readings": len(p.fixes), "ok": ok, "nok": nok})
        log.warning("recount: station '%s': %d readings corrected, %d OK and %d NOK removed",
                    st.name, len(p.fixes), ok, nok)
    if done["readings"]:
        daily.refresh(db, rebuild=True)
    return done
