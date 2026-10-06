"""Stations: what is counted, built from the values of one or more devices.

A device read (a poll, or a record a device pushed) leaves the device's
latest values in Device.last_values. ``device_read`` then records a reading
for every station that uses the device: the station's OK, NOK, total and job
are taken from its source devices' latest values (Station.sources), folded
into the station's reset-proof running totals (app.counters), and logged as a
Reading. Production state, alerts and statistics work on the station.

Parts can also be entered by hand (``add_entry``): a manual entry is a
Reading with manual=True whose raw_pass / raw_fail are the parts entered. It
adds to the station's counters for its job (CounterState.manual_*) and counts
in the statistics and OEE like device data. A station may have no devices at
all and be fed only by manual entries.

``upgrade`` turns the devices of a version 1.4 (or older) database into
stations, once per database.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from . import production
from .counters import SAMPLE_KEYS, Sample, apply_sample, new_state_for
from .models import CounterState, Device, Meta, Reading, Station, utcnow

log = logging.getLogger("cognex.stations")

ROLES = ("ok", "nok", "count", "job")
ROLE_LABELS = {"ok": "OK", "nok": "NOK", "count": "Total", "job": "Job"}
UPGRADE_KEY = "stations_v1"


class MissingValue(Exception):
    """A source device has not supplied the value yet (or is not there)."""


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #


def default_sources(device: Device) -> dict | None:
    """The sources a station gets when it is made for one device: its pass,
    fail, total and job values. None for an OPC UA device without counter
    nodes (its station has to pick them)."""
    if device.protocol == "opcua":
        cfg = device.protocol_config or {}
        node = lambda k: (cfg.get(k) or "").strip() or None  # noqa: E731
        if not node("pass_node") and not node("fail_node"):
            return None
        src = {
            "ok": {"device_id": device.id, "key": node("pass_node")} if node("pass_node") else None,
            "nok": {"device_id": device.id, "key": node("fail_node")} if node("fail_node") else None,
            "count": {"device_id": device.id, "key": node("count_node")} if node("count_node") else None,
            "job": {"device_id": device.id, "key": node("job_node")} if node("job_node") else None,
        }
        return src
    return {role: {"device_id": device.id, "key": key}
            for role, key in (("ok", "pass"), ("nok", "fail"), ("count", "count"), ("job", "job"))}


def station_for_device(device: Device, **fields) -> Station | None:
    sources = default_sources(device)
    if sources is None:
        return None
    default_job = (device.protocol_config or {}).get("default_job") or "MAIN"
    return Station(name=device.name, sources=sources, default_job=str(default_job), **fields)


def keys_for_device(db: Session, device_id: int) -> set[str]:
    """The values the stations need from a device (OPC UA reads only those)."""
    keys = set()
    for st in stations_using(db, device_id):
        for role in ROLES:
            src = st.source(role)
            if src and int(src["device_id"]) == device_id:
                keys.add(str(src["key"]))
    return keys


def stations_using(db: Session, device_id: int) -> list[Station]:
    return [st for st in db.query(Station).order_by(Station.sort_order, Station.name).all()
            if device_id in st.device_ids()]


def check_sources(db: Session, sources: dict) -> dict:
    """Validate and normalise a sources dict from the API. Raises ValueError."""
    out = {}
    for role in ROLES:
        src = (sources or {}).get(role)
        if not src or not src.get("device_id") or src.get("key") in (None, ""):
            out[role] = None
            continue
        device = db.get(Device, int(src["device_id"]))
        if device is None:
            raise ValueError(f"{ROLE_LABELS[role]}: the device does not exist")
        out[role] = {"device_id": device.id, "key": str(src["key"]).strip()}
    if not out["ok"] and not out["nok"] and (out["count"] or out["job"]):
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


def _value(src: dict, devices: dict[int, Device]):
    device = devices.get(int(src["device_id"]))
    if device is None:
        raise MissingValue("its device was deleted")
    vals = device.last_values or {}
    if src["key"] not in vals:
        err = (vals.get("_errors") or {}).get(src["key"])
        raise MissingValue(err or f"no value '{src['key']}' from device '{device.name}' yet")
    return vals[src["key"]]


def _count(value, src: dict) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        raise MissingValue(f"'{src['key']}' holds {value!r}, not a number") from None


def build_sample(station: Station, devices: dict[int, Device]) -> tuple[Sample, bool, bool]:
    """The station's sample from its devices' latest values.

    Returns (sample, linked, own_count): linked when every counter comes from
    the same device (a reset there resets all of them), own_count when there
    is a total counter. Raises MissingValue.
    """
    ok_src, nok_src, count_src, job_src = (station.source(r) for r in ROLES)
    ok = _count(_value(ok_src, devices), ok_src) if ok_src else 0
    nok = _count(_value(nok_src, devices), nok_src) if nok_src else 0
    count = _count(_value(count_src, devices), count_src) if count_src else 0
    job = ""
    if job_src:
        raw = _value(job_src, devices)
        job = "" if raw is None else str(raw).strip()
    counter_devices = {int(s["device_id"]) for s in (ok_src, nok_src, count_src) if s}
    sample = Sample(job_name=job or station.default_job or "MAIN", raw_pass=ok, raw_fail=nok, raw_count=count)
    return sample, len(counter_devices) <= 1, count_src is not None


def _active_state(db: Session, station: Station, sample: Sample, linked: bool, own_count: bool) -> CounterState:
    """Apply a sample to the right CounterState, rotating on job change."""
    active = (
        db.query(CounterState)
        .filter(CounterState.station_id == station.id, CounterState.is_active.is_(True))
        .first()
    )
    if active is None:
        # first ever sample for this station
        active = new_state_for(station, sample)
        db.add(active)
        db.flush()
    elif active.job_name != sample.job_name:
        # job changed: freeze the old totals, start fresh for the new job.
        active.is_active = False
        # Resume a previous run of the same job if one exists, else start new.
        prior = (
            db.query(CounterState)
            .filter(CounterState.station_id == station.id, CounterState.job_name == sample.job_name)
            .first()
        )
        if prior is not None:
            prior.is_active = True
            # treat the station as freshly baselined for the resumed job
            prior.last_raw_pass = sample.raw_pass
            prior.last_raw_fail = sample.raw_fail
            prior.last_raw_count = sample.raw_count
            active = prior
        else:
            active = new_state_for(station, sample)
            db.add(active)
            db.flush()
    else:
        before = active.total_pass
        apply_sample(active, sample, linked=linked, own_count=own_count)
        if active.total_pass > before:
            production.note_pass_increase(station)
    return active


def record(db: Session, station: Station, sample: Sample, linked: bool = True, own_count: bool = True,
           device_id: int | None = None) -> Reading:
    """Accumulate one station sample, log a Reading, notify."""
    from . import notifications

    previous_job = station.current_job
    state = _active_state(db, station, sample, linked, own_count)
    reading = Reading(
        station_id=station.id,
        device_id=device_id,
        job_name=sample.job_name,
        raw_pass=sample.raw_pass,
        raw_fail=sample.raw_fail,
        raw_count=sample.raw_count,
        total_pass=state.total_pass,
        total_fail=state.total_fail,
        extra=sample.extra or {},
        in_production=production.in_production(station),
    )
    db.add(reading)
    station.last_reading_at = utcnow()
    station.current_job = sample.job_name
    db.commit()

    if previous_job is not None and previous_job != sample.job_name:
        notifications.emit(db, "job_change",
                           f"Station '{station.name}' changed job from '{previous_job}' to '{sample.job_name}'",
                           station)
    notifications.evaluate_station(db, station)
    return reading


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
    devices = {d.id: d for d in db.query(Device).filter(Device.id.in_(
        set().union(*(st.device_ids() for st in users)))).all()}
    for station in users:
        try:
            sample, linked, own_count = build_sample(station, devices)
        except MissingValue:
            continue  # shown on the station (status); counted once the value arrives
        ok_src = station.source("ok") or station.source("nok")
        readings.append(record(db, station, sample, linked, own_count, int(ok_src["device_id"])))
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
# Manual entries
# --------------------------------------------------------------------------- #


def _job_state(db: Session, station: Station, job: str) -> CounterState:
    """The counters of a job, made (inactive, or active for a station that
    has none yet) when the job was never counted."""
    state = (db.query(CounterState)
             .filter(CounterState.station_id == station.id, CounterState.job_name == job).first())
    if state is None:
        has_active = (db.query(CounterState.id)
                      .filter(CounterState.station_id == station.id, CounterState.is_active.is_(True)).first())
        state = CounterState(station_id=station.id, job_name=job, total_pass=0, total_fail=0, total_count=0,
                             last_raw_pass=0, last_raw_fail=0, last_raw_count=0, manual_pass=0, manual_fail=0,
                             is_active=has_active is None)
        db.add(state)
        db.flush()
    return state


def _apply_entry(db: Session, station: Station, job: str, ok: int, nok: int) -> None:
    state = _job_state(db, station, job)
    state.manual_pass = (state.manual_pass or 0) + ok
    state.manual_fail = (state.manual_fail or 0) + nok


def _aware(t: dt.datetime) -> dt.datetime:
    return t.replace(tzinfo=dt.timezone.utc) if t.tzinfo is None else t


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
    notifications.evaluate_station(db, station)
    return reading


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
    if not station.device_ids():
        return {"connected": True, "problem": None}
    problems = []
    for role in ROLES:
        src = station.source(role)
        if not src:
            continue
        device = devices.get(int(src["device_id"]))
        if device is None:
            problems.append(f"{ROLE_LABELS[role]}: device deleted")
        elif not device.enabled:
            problems.append(f"{ROLE_LABELS[role]}: device '{device.name}' is disabled")
        elif not device.connected:
            problems.append(f"Device '{device.name}': {device.last_error or 'offline'}")
        else:
            try:
                _value(src, devices)
            except MissingValue as exc:
                problems.append(f"{ROLE_LABELS[role]}: {exc}")
    problems = list(dict.fromkeys(problems))  # one line per device problem
    return {"connected": not problems, "problem": "; ".join(problems) or None}


def devices_of(db: Session, stations: list[Station]) -> dict[int, Device]:
    ids = set().union(*(st.device_ids() for st in stations)) if stations else set()
    return {d.id: d for d in db.query(Device).filter(Device.id.in_(ids)).all()} if ids else {}


# --------------------------------------------------------------------------- #
# Upgrade from 1.4 (devices were the counted unit)
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
    of stations made."""
    with engine.begin() as conn:
        if conn.execute(text("SELECT 1 FROM meta WHERE key = :k"), {"k": UPGRADE_KEY}).first():
            return 0
        if conn.dialect.name == "postgresql":
            for ddl in _PG_LEGACY_DDL:
                conn.execute(text(ddl))
    made = 0
    with Session(bind=engine) as db:
        taken = {n for (n,) in db.query(Station.name)}
        for d in db.query(Device).order_by(Device.id).all():
            if db.get(Station, d.id) is not None:
                continue
            st = station_for_device(
                d, id=d.id, idle_timeout_min=d.idle_timeout_min or production.DEFAULT_IDLE_TIMEOUT_MIN,
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
    return made
