"""Stations: what is counted, from one or more sources.

A station has a list of sources (Station.sources). A source is one device
and which of its values give the OK, NOK, total and job; any of them may be
blank. So a station can have two cameras, each with its own counters and
job, or take OK from one device and NOK from another.

A device read (a poll, or a record a device pushed) leaves the device's
latest values in Device.last_values. ``device_read`` then handles every
source that reads the device:

* Delta: the source's pieces since its previous read, reset-proof
  (app.counters.deltas; SourceState holds the previous raw counters).
* Production (app.production): the station goes into production when a
  source makes ``start_count`` OK pieces within ``start_window_s`` seconds
  (2 within 60 by default), and leaves it when no source's OK has risen for
  the station's idle timeout, or on a manual Stop, which wins until Start.
* Counting: the station's counters only grow while it is in production; then
  each read adds the source's delta. Pieces made while not in production are
  kept for the start window (SourceState.pending): when a start rule fires,
  the pieces of every source within that window count too, so the pieces
  that started production are counted. Older ones never count.
* Jobs: a source counts under its own job value, or, without one, under the
  station's job (the job of its first source with a job value, else
  default_job). The CounterStates of the jobs being run now are active.
* Every read is logged as a Reading (the source's raw values, the totals of
  its job, and ok_added / nok_added: the pieces this read counted), so the raw
  device data are kept even while nothing is counted.

Parts can also be entered by hand (``add_entry``): a manual entry is a
Reading with manual=True whose raw_pass / raw_fail are the parts entered. It
adds to the station's counters for its job (CounterState.manual_*) and counts
in the statistics and OEE like device data. A station may have no devices at
all and be fed only by manual entries. An entry may be negative (a
correction, e.g. to take back falsely counted parts): it subtracts wherever
entries count, and never fires a scrap alert (``below_zero`` says when it
would take a day's count below zero, which the API has confirmed).

``upgrade`` turns the devices of a version 1.4 (or older) database into
stations, and ``upgrade_sources`` the role-based sources of 1.5 to 1.7 into
the list of sources, once per database each.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from . import mute, pieces, production
from .counters import SAMPLE_KEYS, deltas
from .models import CounterState, Device, Job, Meta, Reading, SourceState, Station, utcnow

log = logging.getLogger("cognex.stations")

ROLES = ("ok", "nok", "count", "job")
ROLE_LABELS = {"ok": "OK", "nok": "NOK", "count": "Total", "job": "Job"}
UPGRADE_KEY = "stations_v1"
SOURCES_KEY = "stations_v2"
MAX_SOURCES = 20

# one station's reads are recorded one at a time (polls and listeners run in
# different threads, and two devices of a station may report together)
_locks: dict[int, threading.Lock] = defaultdict(threading.Lock)
_locks_guard = threading.Lock()


def _lock(station_id: int) -> threading.Lock:
    with _locks_guard:
        return _locks[station_id]


class MissingValue(Exception):
    """A source device has not supplied the value yet (or is not there)."""


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #


def new_source(device_id: int, sid: str = "s1", start_count: int = production.DEFAULT_START_COUNT,
               start_window_s: int = production.DEFAULT_START_WINDOW_S, **keys) -> dict:
    return {"id": sid, "device_id": int(device_id),
            **{r: (str(keys[r]).strip() or None) if keys.get(r) not in (None, "") else None for r in ROLES},
            "start_count": int(start_count), "start_window_s": int(start_window_s)}


def normalize_sources(raw, start_count: int = production.DEFAULT_START_COUNT) -> list[dict]:
    """Station.sources as a list of sources, also from the role dict of 1.5 to
    1.7 ({"ok": {"device_id": 1, "key": "pass"}, ...}): there each device
    becomes a source with the roles it supplied, so OK from device 1 and NOK
    from device 2 become two sources and count as before. ``start_count`` is
    the start rule such converted sources get."""
    if not raw:
        return []
    out: list[dict] = []
    if isinstance(raw, dict):
        by_device: dict[int, dict] = {}
        for role in ROLES:
            src = raw.get(role)
            if not isinstance(src, dict) or not src.get("device_id") or src.get("key") in (None, ""):
                continue
            did = int(src["device_id"])
            if did not in by_device:
                by_device[did] = new_source(did, f"s{len(out) + 1}", start_count=start_count)
                out.append(by_device[did])
            by_device[did][role] = str(src["key"])
        return out
    taken: set[str] = set()
    for i, s in enumerate(raw):
        if not isinstance(s, dict) or not s.get("device_id"):
            continue
        sid = str(s.get("id") or "").strip()[:16]
        if not sid or sid in taken:
            sid = next(f"s{n}" for n in range(i + 1, i + 1000) if f"s{n}" not in taken
                       and f"s{n}" not in {str(x.get("id")) for x in raw if isinstance(x, dict)})
        taken.add(sid)
        out.append(new_source(s["device_id"], sid,
                              start_count=s.get("start_count") or production.DEFAULT_START_COUNT,
                              start_window_s=s.get("start_window_s") or production.DEFAULT_START_WINDOW_S,
                              **{r: s.get(r) for r in ROLES}))
    return out


def default_sources(device: Device) -> list[dict] | None:
    """The sources a station gets when it is made for one device: its pass,
    fail, total and job values. None for an OPC UA device without counter
    nodes (its station has to pick them)."""
    if device.protocol == "opcua":
        cfg = device.protocol_config or {}
        node = lambda k: (cfg.get(k) or "").strip() or None  # noqa: E731
        if not node("pass_node") and not node("fail_node"):
            return None
        return [new_source(device.id, ok=node("pass_node"), nok=node("fail_node"),
                           count=node("count_node"), job=node("job_node"))]
    return [new_source(device.id, ok="pass", nok="fail", count="count", job="job")]


def station_for_device(device: Device, start_count: int = production.DEFAULT_START_COUNT,
                       **fields) -> Station | None:
    sources = default_sources(device)
    if sources is None:
        return None
    for s in sources:
        s["start_count"] = start_count
    default_job = (device.protocol_config or {}).get("default_job") or "MAIN"
    return Station(name=device.name, sources=sources, default_job=str(default_job), **fields)


def keys_for_device(db: Session, device_id: int) -> set[str]:
    """The values the stations need from a device (OPC UA reads only those)."""
    keys = set()
    for st in stations_using(db, device_id):
        for src in st.source_list():
            if src["device_id"] == device_id:
                keys.update(src[r] for r in ROLES if src[r])
    return keys


def stations_using(db: Session, device_id: int) -> list[Station]:
    return [st for st in db.query(Station).order_by(Station.sort_order, Station.name).all()
            if device_id in st.device_ids()]


def check_sources(db: Session, sources) -> list[dict]:
    """Validate and normalise the sources from the API (a list, or the role
    dict of 1.5 to 1.7). Raises ValueError."""
    out = normalize_sources(sources)
    if len(out) > MAX_SOURCES:
        raise ValueError(f"A station can have at most {MAX_SOURCES} sources")
    for n, src in enumerate(out, 1):
        device = db.get(Device, src["device_id"])
        what = f"Source {n}"
        if device is None:
            raise ValueError(f"{what}: the device does not exist")
        what = f"Source {n} ({device.name})"
        if not any(src[r] for r in ROLES):
            raise ValueError(f"{what}: pick at least one value (OK, NOK or job), or remove the source")
        if src["count"] and not (src["ok"] or src["nok"]):
            raise ValueError(f"{what}: a total needs the OK or the NOK count of the same device")
        if not 1 <= src["start_count"] <= 100_000:
            raise ValueError(f"{what}: the start rule needs 1 to 100000 OK pieces")
        if not 1 <= src["start_window_s"] <= 86_400:
            raise ValueError(f"{what}: the start window is 1 s to 24 h")
    if out and not any(s["ok"] or s["nok"] for s in out):
        raise ValueError("Pick where the OK or the NOK count comes from (at least one of them), "
                         "or no device at all for a station fed by manual entries")
    return out


def value_choices(device: Device) -> list[dict]:
    """The values a station can take from a device (OPC UA: any node, picked
    with Browse; its known ones are listed too)."""
    vals = device.last_values or {}
    from . import protocols

    cls = protocols.available().get(device.protocol)
    if cls is not None and getattr(cls, "any_value", False):
        return [{"key": k, "label": k, "value": v} for k, v in vals.items() if not k.startswith("_")]
    return [{"key": k, "label": label, "value": vals.get(k)} for k, label in SAMPLE_KEYS.items()]


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #


def _value(device: Device | None, key: str):
    if device is None:
        raise MissingValue("its device was deleted")
    vals = device.last_values or {}
    if key not in vals:
        err = (vals.get("_errors") or {}).get(key)
        raise MissingValue(err or f"no value '{key}' from device '{device.name}' yet")
    return vals[key]


def _count(value, key: str) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        raise MissingValue(f"'{key}' holds {value!r}, not a number") from None


def source_values(src: dict, devices: dict[int, Device]) -> tuple[int | None, int | None, int | None, str | None]:
    """(ok, nok, count, job) of a source from its device's latest values;
    None for the values it doesn't read. Raises MissingValue."""
    device = devices.get(src["device_id"])
    ok, nok, count = (_count(_value(device, src[r]), src[r]) if src[r] else None for r in ("ok", "nok", "count"))
    job = None
    if src["job"]:
        raw = _value(device, src["job"])
        job = None if raw is None else (str(raw).strip() or None)
    return ok, nok, count, job


def _signature(src: dict) -> str:
    return "|".join(str(src[k] or "") for k in ("device_id", "ok", "nok", "count"))


def _states(db: Session, station: Station) -> dict[str, SourceState]:
    return {s.source_id: s for s in db.query(SourceState).filter(SourceState.station_id == station.id)}


def station_job(station: Station, states: dict[str, SourceState]) -> str | None:
    """The job of sources without a job value: the job of the station's first
    source with one (None until that source has been read), else the default
    job."""
    for src in station.source_list():
        if src["job"]:
            st = states.get(src["id"])
            return st.job if st is not None and st.job else None
    return station.default_job or "MAIN"


def _job_state(db: Session, station: Station, job: str, active: bool | None = None) -> CounterState:
    """The counters of a job, made when the job was never counted: active when
    ``active`` says so, else only when the station has no active job yet."""
    state = (db.query(CounterState)
             .filter(CounterState.station_id == station.id, CounterState.job_name == job).first())
    if state is None:
        if active is None:
            active = (db.query(CounterState.id)
                      .filter(CounterState.station_id == station.id, CounterState.is_active.is_(True))
                      .first()) is None
        state = CounterState(station_id=station.id, job_name=job, total_pass=0, total_fail=0, total_count=0,
                             last_raw_pass=0, last_raw_fail=0, last_raw_count=0, manual_pass=0, manual_fail=0,
                             is_active=active)
        db.add(state)
        db.flush()
    return state


def _aware(t: dt.datetime | None) -> dt.datetime | None:
    return t.replace(tzinfo=dt.timezone.utc) if t is not None and t.tzinfo is None else t


@dataclass
class _Read:
    """One source's read, before it is logged."""

    src: dict
    job: str
    raw: tuple[int, int, int]
    added: list[int] = field(default_factory=lambda: [0, 0, 0])  # ok, nok, total
    job_changed_from: str | None = None
    # pieces of the previous job's open piece, closed by the job change
    closed_old: list[int] | None = None
    # the start rule fired: other sources' pending pieces that count with it,
    # each under its own job: (source, its state, [ok, nok, total])
    flushed: list[tuple[dict, SourceState, list[int]]] = field(default_factory=list)


def _process(db: Session, station: Station, src: dict, devices: dict[int, Device],
             states: dict[str, SourceState], now: dt.datetime) -> _Read:
    """Work out a source's delta and whether it counts. Raises MissingValue."""
    ok, nok, count, job_value = source_values(src, devices)
    job = job_value or station_job(station, states)
    if job is None:
        raise MissingValue("waiting for the job from the station's first source with a job value")
    st = states.get(src["id"])
    if st is None:
        st = SourceState(station_id=station.id, source_id=src["id"], baselined=False, pending=[])
        db.add(st)
        states[src["id"]] = st
    sig = _signature(src)
    if st.signature != sig:  # new source, or edited to read something else
        st.signature, st.baselined, st.pending = sig, False, []
    read = _Read(src, job, (ok or 0, nok or 0, count if count is not None else (ok or 0) + (nok or 0)))
    if st.job and st.job != job:
        read.job_changed_from = st.job
    st.job = job
    from_zero = bool((devices[src["device_id"]].last_values or {}).get("_from_zero"))
    if st.baselined or from_zero:
        prev = (st.last_pass, st.last_fail, st.last_count) if st.baselined else (0, 0, 0)
        d_ok, d_nok, d_count = deltas(prev, (ok or 0, nok or 0, count or 0))
        if count is None:
            d_count = d_ok + d_nok
    else:
        d_ok = d_nok = d_count = 0  # the first read is the baseline (see sample_values)
    st.last_pass, st.last_fail, st.last_count, st.baselined = ok or 0, nok or 0, count or 0, True

    # piece rule of the job: pictures become pieces (app.pieces)
    state = production.state(station, now)
    if read.job_changed_from and st.open_piece:
        a, b = pieces.close_open(_piece_rule(db, read.job_changed_from), st.open_piece)
        st.open_piece = None
        if (a or b) and state == production.RUNNING:
            read.closed_old = [a, b, a + b]
    rule = _piece_rule(db, job)
    if state == production.STOPPED:
        st.open_piece = None  # nothing counts while stopped
    elif rule:
        piece_id = None
        if rule.get("group_key"):
            piece_id = (devices[src["device_id"]].last_values or {}).get(rule["group_key"])
            piece_id = None if piece_id in (None, "") else str(piece_id)
        d_ok, d_nok, st.open_piece = pieces.apply(rule, st.open_piece, d_ok, d_nok, now.timestamp(), piece_id)
        d_count = d_ok + d_nok
    elif st.open_piece:
        st.open_piece = None  # the rule was removed
    if state == production.RUNNING:
        st.pending = []
        read.added = [d_ok, d_nok, d_count]
        if d_ok > 0:
            production.note_pass_increase(station, now)
            st.last_ok_at = now
    elif state == production.IDLE:
        t = now.timestamp()
        window = src["start_window_s"]
        pending = [p for p in (st.pending or []) if t - p[0] <= window]
        if d_ok or d_nok or d_count:
            pending.append([t, d_ok, d_nok, d_count])
        st.pending = pending
        if src["ok"] and sum(p[1] for p in pending) >= src["start_count"]:
            # the start rule fired: the pieces in the window count, also those
            # of the station's other sources (the same burst of production)
            production.note_pass_increase(station, now)
            st.last_ok_at = now
            read.added = [sum(p[i] for p in pending) for i in (1, 2, 3)]
            st.pending = []
            by_id = {x["id"]: x for x in station.source_list()}
            for other in states.values():
                if other is st:
                    continue
                held = [sum(p[i] for p in other.pending or [] if t - p[0] <= window) for i in (1, 2, 3)]
                other.pending = []
                if any(held) and other.source_id in by_id and other.job:
                    read.flushed.append((by_id[other.source_id], other, held))
    else:  # stopped by an operator: nothing counts
        st.pending = []
    return read


def _piece_rule(db: Session, job: str | None) -> dict | None:
    if not job:
        return None
    return db.query(Job.piece_rule).filter(Job.name == job).scalar()


def _set_active_jobs(db: Session, station: Station, states: dict[str, SourceState]) -> None:
    jobs = {s.job for s in states.values() if s.job}
    for cs in db.query(CounterState).filter(CounterState.station_id == station.id):
        cs.is_active = cs.job_name in jobs
        jobs.discard(cs.job_name)
    for job in jobs:
        _job_state(db, station, job, active=True)


def is_active(station: Station, src: dict, state: SourceState | None, devices: dict[int, Device],
              now: dt.datetime | None = None) -> bool:
    """A source is active while the station is in production and its OK rose
    within the idle timeout (a source without an OK value: while the station
    is in production) and its device is online."""
    now = now or utcnow()
    device = devices.get(src["device_id"])
    if not production.in_production(station, now) or device is None or not device.connected:
        return False
    if not src["ok"]:
        return bool(src["nok"] or src["count"])
    last = _aware(state.last_ok_at) if state else None
    timeout = max(1, station.idle_timeout_min or production.DEFAULT_IDLE_TIMEOUT_MIN)
    return last is not None and (now - last).total_seconds() < timeout * 60


def _primary_job(station: Station, states: dict[str, SourceState], devices: dict[int, Device],
                 now: dt.datetime) -> str | None:
    """Station.current_job: the job of its first active source, else of the
    source that was active last, else of its first source."""
    srcs = [(s, states.get(s["id"])) for s in station.source_list()]
    srcs = [(s, st) for s, st in srcs if st is not None and st.job]
    if not srcs:
        return None
    for s, st in srcs:
        if (s["ok"] or s["nok"]) and is_active(station, s, st, devices, now):
            return st.job
    last = max(srcs, key=lambda x: _aware(x[1].last_ok_at) or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
    return last[1].job if last[1].last_ok_at else srcs[0][1].job


def record_device(db: Session, station: Station, device: Device, devices: dict[int, Device]) -> list[Reading]:
    """Handle the sources of ``station`` that read ``device``; log a Reading
    for each, notify. The caller holds the station's lock."""
    from . import notifications

    now = utcnow()
    states = _states(db, station)
    ids = {src["id"] for src in station.source_list()}
    for sid in set(states) - ids:  # a source that was removed from the station
        db.delete(states.pop(sid))
    reads = []
    for src in station.source_list():
        if src["device_id"] != device.id:
            continue
        try:
            reads.append(_process(db, station, src, devices, states, now))
        except MissingValue:
            continue  # shown on the station (status); counted once the value arrives
    if not reads:
        db.rollback()
        return []
    _set_active_jobs(db, station, states)
    running = production.in_production(station, now)
    readings = []

    def log(src: dict, job: str, raw: tuple[int, int, int], added: list[int]) -> None:
        cs = _job_state(db, station, job, active=True)
        ok, nok, total = added
        cs.total_pass += ok
        cs.total_fail += nok
        cs.total_count += total
        reading = Reading(
            station_id=station.id, device_id=src["device_id"], source_id=src["id"], job_name=job,
            raw_pass=raw[0], raw_fail=raw[1], raw_count=raw[2],
            total_pass=cs.total_pass, total_fail=cs.total_fail, ok_added=ok, nok_added=nok,
            in_production=running, created_at=now,
        )
        db.add(reading)
        readings.append(reading)

    for read in reads:
        if read.closed_old:  # the last piece of the previous job
            log(read.src, read.job_changed_from, read.raw, read.closed_old)
        log(read.src, read.job, read.raw, read.added)
        for src, other, pieces in read.flushed:  # counted with the start, under their own job
            log(src, other.job, (other.last_pass, other.last_fail, other.last_count), pieces)
    station.last_reading_at = now
    station.current_job = _primary_job(station, states, devices, now) or station.current_job
    db.commit()

    several = len(station.source_list()) > 1
    for read in reads:
        if read.job_changed_from:
            mute.job_changed(db, station, read.job_changed_from, read.job)  # "!mute" lasts until here
            who = f"Station '{station.name}'" + (f" (device '{device.name}')" if several else "")
            notifications.emit(db, "job_change",
                               f"{who} changed job from '{read.job_changed_from}' to '{read.job}'", station)
    notifications.evaluate_station(db, station)
    return readings


def device_read(db: Session, device: Device, values: dict) -> list[Reading]:
    """A device was read: keep its values and record its stations."""
    errors = values.get("_errors") or {}
    device.last_values = values
    device.connected = True
    device.last_error = "; ".join(errors.values()) if errors else None
    device.last_poll_at = utcnow()
    if values.get("job") not in (None, ""):
        device.current_job = str(values["job"])
    db.commit()
    readings = []
    users = stations_using(db, device.id)
    if not users:
        return readings
    for station in users:
        with _lock(station.id):
            db.refresh(station)
            devices = devices_of(db, [station])
            devices[device.id] = device
            readings += record_device(db, station, device, devices)
    return readings


def device_failed(db: Session, device: Device, error: str) -> None:
    """A read failed: mark the device offline and tell its stations' rules."""
    from . import notifications

    device.connected = False
    device.last_error = error
    device.last_poll_at = utcnow()
    db.commit()
    for station in stations_using(db, device.id):
        try:
            notifications.evaluate_station(db, station)
        except Exception:  # noqa: BLE001
            db.rollback()


# --------------------------------------------------------------------------- #
# What the dashboard shows
# --------------------------------------------------------------------------- #


@dataclass
class Shown:
    """The counters of the jobs a station runs now, since the last reset from
    the dashboard (CounterState.shown_*), added up over those jobs."""

    jobs: list[str]
    shown_pass: int
    shown_fail: int
    shown_count: int
    reset_at: dt.datetime | None

    @property
    def job_name(self) -> str:
        return " + ".join(self.jobs)

    @property
    def shown_scrap_rate(self) -> float:
        return self.shown_fail / self.shown_count if self.shown_count > 0 else 0.0


def active_states(db: Session, station: Station) -> list[CounterState]:
    """The CounterStates of the jobs the station runs now, its current job first."""
    rows = (db.query(CounterState)
            .filter(CounterState.station_id == station.id, CounterState.is_active.is_(True)).all())
    return sorted(rows, key=lambda s: (s.job_name != station.current_job, s.job_name))


def shown(db: Session, station: Station) -> Shown | None:
    rows = active_states(db, station)
    if not rows:
        return None
    resets = [_aware(s.reset_at) for s in rows if s.reset_at]
    return Shown([s.job_name for s in rows], sum(s.shown_pass for s in rows), sum(s.shown_fail for s in rows),
                 sum(s.shown_count for s in rows), max(resets) if resets else None)


def current_jobs(db: Session, station: Station) -> list[str]:
    """The jobs the station's sources run now (its current job first)."""
    return [s.job_name for s in active_states(db, station)] or ([station.current_job] if station.current_job else [])


def sources_view(db: Session, station: Station, devices: dict[int, Device], now: dt.datetime | None = None) -> dict:
    """Each source with its device, job and whether it is active, plus the
    names of the active devices, or the device that was active last."""
    now = now or utcnow()
    states = _states(db, station)
    rows = []
    for src in station.source_list():
        st = states.get(src["id"])
        device = devices.get(src["device_id"])
        rows.append({
            **src,
            "device": device.name if device else None,
            "connected": bool(device and device.connected),
            "current_job": st.job if st else None,  # "job" is the value it reads it from
            "active": is_active(station, src, st, devices, now),
            "last_ok_at": _aware(st.last_ok_at) if st else None,
            "open_piece": st.open_piece if st else None,
        })
    active = list(dict.fromkeys(r["device"] for r in rows if r["active"] and r["device"]))
    last = None
    if not active:
        seen = [r for r in rows if r["last_ok_at"] and r["device"]]
        if seen:
            r = max(seen, key=lambda r: r["last_ok_at"])
            last = {"device": r["device"], "at": r["last_ok_at"]}
    return {"sources": rows, "active_devices": active, "last_active": last}


# --------------------------------------------------------------------------- #
# Manual entries
# --------------------------------------------------------------------------- #


def _apply_entry(db: Session, station: Station, job: str, ok: int, nok: int) -> None:
    state = _job_state(db, station, job)
    state.manual_pass = (state.manual_pass or 0) + ok
    state.manual_fail = (state.manual_fail or 0) + nok


def add_entry(db: Session, station: Station, ok: int, nok: int, at: dt.datetime | None = None,
              job: str | None = None, note: str | None = None, user: str | None = None) -> Reading:
    """Parts entered by hand. Counts like device data from ``at`` (now by default)."""
    from . import notifications

    at = _aware(at) if at else utcnow()
    job = (job or "").strip() or station.current_job or station.default_job or "MAIN"
    _apply_entry(db, station, job, ok, nok)
    reading = Reading(station_id=station.id, job_name=job, raw_pass=ok, raw_fail=nok, raw_count=ok + nok,
                      total_pass=0, total_fail=0, manual=True, note=(note or "").strip() or None,
                      entered_by=user, in_production=True, created_at=at)
    db.add(reading)
    if ok > 0:
        last = station.last_pass_change_at
        if last is None or _aware(last) < at:
            production.note_pass_increase(station, min(at, utcnow()))
    if station.current_job is None:
        station.current_job = job
    db.commit()
    if ok < 0 or nok < 0:
        # a correction takes counts back: it never fires a scrap alert
        notifications.check_production(db, station)
    else:
        notifications.evaluate_station(db, station)
    return reading


def below_zero(db: Session, station: Station, job: str | None, ok: int, nok: int, at: dt.datetime | None,
               tz: dt.tzinfo, skip_id: int | None = None) -> list[str]:
    """What an entry of ``ok`` / ``nok`` parts at ``at`` would take below zero
    on its (local) day: the station's or the job's OK or NOK, counting every
    part of that day (devices and entries, also excluded ones). ``skip_id``:
    the entry being changed, left out. Empty when nothing would."""
    from .scrap_stats import day_bounds, station_parts

    if ok >= 0 and nok >= 0:
        return []
    at = _aware(at) if at else utcnow()
    job = (job or "").strip() or station.current_job or station.default_job or "MAIN"
    day = at.astimezone(tz).date()
    start, end = day_bounds(day, day, tz)
    st_ok = st_nok = job_ok = job_nok = 0
    for r in station_parts(db, station, start, end):
        if skip_id is not None and r["id"] == skip_id:
            continue
        st_ok, st_nok = st_ok + r["ok"], st_nok + r["nok"]
        if r["job"] == job:
            job_ok, job_nok = job_ok + r["ok"], job_nok + r["nok"]
    out = []
    for what, have_ok, have_nok in ((f"Station '{station.name}'", st_ok, st_nok), (f"Job '{job}'", job_ok, job_nok)):
        low = [f"{name} {have + add}" for name, have, add in (("OK", have_ok, ok), ("NOK", have_nok, nok))
               if add < 0 and have + add < 0]
        if low:
            out.append(f"{what} would end {day.isoformat()} at " + " and ".join(low))
    return out


def update_entry(db: Session, reading: Reading, ok: int, nok: int, at: dt.datetime | None,
                 job: str | None, note: str | None) -> Reading:
    station = reading.station
    _apply_entry(db, station, reading.job_name, -reading.raw_pass, -reading.raw_fail)
    job = (job or "").strip() or reading.job_name
    _apply_entry(db, station, job, ok, nok)
    reading.job_name, reading.raw_pass, reading.raw_fail, reading.raw_count = job, ok, nok, ok + nok
    if at:
        reading.created_at = _aware(at)
    reading.note = (note or "").strip() or None
    db.commit()
    return reading


def delete_entry(db: Session, reading: Reading) -> None:
    _apply_entry(db, reading.station, reading.job_name, -reading.raw_pass, -reading.raw_fail)
    db.delete(reading)
    db.commit()


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #


def status(station: Station, devices: dict[int, Device]) -> dict:
    """Online when every source device is; the problem otherwise. A station
    without devices (manual entries only) is always online."""
    srcs = station.source_list()
    if not srcs:
        return {"connected": True, "problem": None}
    problems = []
    for src in srcs:
        device = devices.get(src["device_id"])
        if device is None:
            problems.append("A source's device was deleted")
        elif not device.enabled:
            problems.append(f"Device '{device.name}' is disabled")
        elif not device.connected:
            problems.append(f"Device '{device.name}': {device.last_error or 'offline'}")
        else:
            for role in ROLES:
                if src[role]:
                    try:
                        _value(device, src[role])
                    except MissingValue as exc:
                        problems.append(f"{ROLE_LABELS[role]}: {exc}")
    problems = list(dict.fromkeys(problems))  # one line per device problem
    return {"connected": not problems, "problem": "; ".join(problems) or None}


def devices_of(db: Session, stations: list[Station]) -> dict[int, Device]:
    ids = set().union(*(st.device_ids() for st in stations)) if stations else set()
    return {d.id: d for d in db.query(Device).filter(Device.id.in_(ids)).all()} if ids else {}


# --------------------------------------------------------------------------- #
# Upgrades: from 1.4 (devices were the counted unit) and from 1.5 - 1.7 (one
# device per role)
# --------------------------------------------------------------------------- #

_PG_LEGACY_DDL = (
    "ALTER TABLE readings ALTER COLUMN device_id DROP NOT NULL",
    "ALTER TABLE readings DROP CONSTRAINT IF EXISTS readings_device_id_fkey",
    "ALTER TABLE counter_states ALTER COLUMN device_id DROP NOT NULL",
    "ALTER TABLE counter_states DROP CONSTRAINT IF EXISTS counter_states_device_id_fkey",
    "ALTER TABLE counter_states DROP CONSTRAINT IF EXISTS uq_device_job",
    "ALTER TABLE notification_rules DROP CONSTRAINT IF EXISTS notification_rules_device_id_fkey",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_station_job ON counter_states (station_id, job_name)",
)


def upgrade(engine: Engine) -> int:
    """Give every device of a 1.4 database its own station with the same id,
    so the dashboard looks as before and readings, counters and alert rules
    keep their numbers. Runs once per database (recorded in the meta table;
    restoring an older backup runs it again on that data). Returns the number
    of stations made. Then runs ``upgrade_sources``."""
    with engine.begin() as conn:
        done = conn.execute(text("SELECT 1 FROM meta WHERE key = :k"), {"k": UPGRADE_KEY}).first()
        if not done and conn.dialect.name == "postgresql":
            for ddl in _PG_LEGACY_DDL:
                conn.execute(text(ddl))
    made = 0
    if not done:
        with Session(bind=engine) as db:
            taken = {n for (n,) in db.query(Station.name)}
            for d in db.query(Device).order_by(Device.id).all():
                if db.get(Station, d.id) is not None:
                    continue
                st = station_for_device(
                    d, start_count=1, id=d.id,
                    idle_timeout_min=d.idle_timeout_min or production.DEFAULT_IDLE_TIMEOUT_MIN,
                    manual_stop=bool(d.manual_stop), last_pass_change_at=d.last_pass_change_at,
                    notified_state=d.notified_state, current_job=d.current_job,
                    stats_default=d.stats_default or "include", last_reading_at=d.last_poll_at,
                    created_at=d.created_at or utcnow(),
                )
                if st is None:  # an OPC UA device without nodes: nothing was counted
                    continue
                if st.name in taken:
                    st.name = f"{st.name} ({d.id})"
                taken.add(st.name)
                db.add(st)
                made += 1
            db.flush()
            for table in ("counter_states", "readings", "notification_rules"):
                db.execute(text(f"UPDATE {table} SET station_id = device_id WHERE station_id IS NULL "
                                "AND device_id IN (SELECT id FROM stations)"))
            db.merge(Meta(key=UPGRADE_KEY, value=utcnow().isoformat()))
            db.commit()
        if made:
            from .backup import reset_sequences

            with engine.begin() as conn:
                reset_sequences(conn)
            log.info("upgrade: made %d stations from devices", made)
    upgrade_sources(engine)
    return made


def upgrade_sources(engine: Engine) -> int:
    """Turn the role dict of 1.5 to 1.7 into a list of sources (a source per
    device), once per database. The sources start production on any OK piece
    (start_count 1) as before, and carry on from the raw counters the station
    read last, so no reading is lost at the upgrade. Returns how many
    stations were changed."""
    changed = 0
    with Session(bind=engine) as db:
        if db.get(Meta, SOURCES_KEY) is not None:
            return 0
        for st in db.query(Station).all():
            if not isinstance(st.sources, dict):
                continue
            old = st.sources
            st.sources = normalize_sources(old, start_count=1)
            changed += 1
            active = (db.query(CounterState)
                      .filter(CounterState.station_id == st.id, CounterState.is_active.is_(True)).first())
            if active is None:
                continue
            for src in st.sources:
                # raw values per role as the station last read them
                raw = {"ok": active.last_raw_pass or 0, "nok": active.last_raw_fail or 0,
                       "count": active.last_raw_count or 0}
                db.add(SourceState(
                    station_id=st.id, source_id=src["id"], signature=_signature(src), baselined=True,
                    last_pass=raw["ok"] if src["ok"] else 0, last_fail=raw["nok"] if src["nok"] else 0,
                    last_count=raw["count"] if src["count"] else 0, job=active.job_name,
                    last_ok_at=st.last_pass_change_at if src["ok"] else None, pending=[]))
        db.merge(Meta(key=SOURCES_KEY, value=utcnow().isoformat()))
        db.commit()
    if changed:
        log.info("upgrade: %d stations now have a list of sources", changed)
    return changed
