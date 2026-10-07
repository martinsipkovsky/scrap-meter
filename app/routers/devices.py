"""Device CRUD (the connections stations read from), protocol metadata,
manual poll/test, export/import of devices and stations, and the OPC UA
Browse helper.

Passwords in a protocol config (e.g. an OPC UA login) never leave the server:
the API and export files show SECRET_MASK instead, and saving SECRET_MASK
keeps the stored password."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import jobs, protocols, stations
from ..config import settings
from ..database import get_db
from ..dependencies import require_api_user, require_permission
from ..models import CounterState, Device, Job, Station, User
from ..poller import read_device
from ..protocols import opcua
from ..protocols.base import ProtocolError
from ..protocols.tcp_listener import parse_port_range
from ..schemas import (DeviceCreate, DeviceExportItem, DeviceImport, DeviceOut, DeviceUpdate,
                       StationExportItem)

router = APIRouter(prefix="/api/devices", tags=["devices"])

SECRET_KEYS = ("password",)
SECRET_MASK = "********"


def masked_config(config: dict | None) -> dict:
    cfg = dict(config or {})
    for k in SECRET_KEYS:
        if cfg.get(k):
            cfg[k] = SECRET_MASK
    return cfg


def _keep_secrets(new: dict, old: dict | None) -> dict:
    """A masked password in a saved config means: keep the stored one."""
    new = dict(new or {})
    for k in SECRET_KEYS:
        if new.get(k) == SECRET_MASK:
            if (old or {}).get(k):
                new[k] = old[k]
            else:
                new.pop(k)
    return new


def _device_out(d: Device) -> DeviceOut:
    out = DeviceOut.model_validate(d)
    out.protocol_config = masked_config(d.protocol_config)
    return out


def _normalise(data: dict, protocol: str) -> None:
    """OPC UA devices keep their address in the endpoint URL; host and port
    are taken from it so the device list shows where the server is."""
    if protocol != "opcua":
        return
    url = (data.get("protocol_config") or {}).get("endpoint")
    if url:
        try:
            data["host"], data["port"] = opcua.split_endpoint(url)
        except ProtocolError as exc:
            raise HTTPException(400, str(exc)) from exc


@router.get("/protocols")
def protocol_catalog(_: User = Depends(require_api_user)):
    return protocols.describe()


@router.get("/listen-ports")
def listen_ports(_: User = Depends(require_api_user)):
    """Port range published for devices that push data to the app."""
    lo, hi = parse_port_range(settings.listen_ports)
    return {"first": lo, "last": hi}


def _check_listen_port(db: Session, protocol: str, port: int, device_id: int | None = None) -> None:
    """A pushing device needs a free port inside the published range."""
    if not protocols.is_push(protocol):
        return
    lo, hi = parse_port_range(settings.listen_ports)
    if not lo <= port <= hi:
        raise HTTPException(
            400,
            f"Listen port must be between {lo} and {hi} (the ports published in "
            f"docker-compose, LISTEN_PORTS). Got {port}.",
        )
    from ..powerbi_access import reserved_port

    if port == reserved_port():
        raise HTTPException(409, f"Port {port} is used for Power BI (Database tab). Pick another one.")
    clash = (
        db.query(Device)
        .filter(
            Device.protocol.in_(protocols.push_keys()),
            Device.port == port,
            Device.id != (device_id or 0),
        )
        .first()
    )
    if clash:
        raise HTTPException(409, f"Port {port} is already used by device '{clash.name}'")


DEVICE_FIELDS = ("name", "host", "port", "protocol", "protocol_config", "poll_interval", "enabled")
STATION_FIELDS = tuple(f for f in StationExportItem.model_fields if f not in ("sources", "ideal_cycle_s"))


class OpcUaBrowse(BaseModel):
    protocol_config: dict = {}
    node_id: str | None = None
    # the device being edited: its stored password is used for a masked one
    device_id: int | None = None


@router.post("/opcua/browse")
def opcua_browse(
    payload: OpcUaBrowse,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("manage_devices")),
):
    """List a node's children on an OPC UA server, to pick node ids. With
    only a device id (station form) the device's saved settings are used."""
    device = db.get(Device, payload.device_id) if payload.device_id else None
    if device is not None and not payload.protocol_config:
        cfg = dict(device.protocol_config or {})
    else:
        cfg = _keep_secrets(payload.protocol_config, device.protocol_config if device else None)
    try:
        url = opcua.endpoint_url("", 0, cfg)
        opcua.split_endpoint(url)
        return opcua.browse(url, cfg, payload.node_id)
    except ProtocolError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/opcua/certificate")
def opcua_certificate(_: User = Depends(require_permission("manage_devices"))):
    """The app's OPC UA client certificate, to trust on the server."""
    return Response(
        opcua.certificate_der(),
        media_type="application/pkix-cert",
        headers={"Content-Disposition": 'attachment; filename="scrap-meter-opcua-client.der"'},
    )


@router.get("/export")
def export_devices(db: Session = Depends(get_db), _: User = Depends(require_permission("view_dashboard"))):
    """All devices, stations and job cycle times as a downloadable JSON file
    (no counter history, no passwords). Devices keep the list name "cameras"
    of earlier versions; stations name their devices."""
    devices = db.query(Device).order_by(Device.name).all()
    names = {d.id: d.name for d in devices}
    cameras = [
        {**{f: getattr(d, f) for f in DEVICE_FIELDS}, "protocol_config": masked_config(d.protocol_config)}
        for d in devices
    ]
    out_stations = []
    for st in db.query(Station).order_by(Station.sort_order, Station.name).all():
        sources = [{"device": names.get(src["device_id"], "?"),
                    **{k: src[k] for k in (*stations.ROLES, "start_count", "start_window_s")}}
                   for src in st.source_list()]
        out_stations.append({**{f: getattr(st, f) for f in STATION_FIELDS}, "sources": sources})
    jobs.sync(db)
    out_jobs = [{"name": j.name, "ideal_cycle_s": j.ideal_cycle_s} for j in db.query(Job).order_by(Job.name)]
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    return JSONResponse(
        {"version": 4, "exported_at": dt.datetime.now(dt.timezone.utc).isoformat(),
         "cameras": cameras, "stations": out_stations, "jobs": out_jobs},
        headers={"Content-Disposition": f'attachment; filename="scrap-meter-devices-{stamp}.json"'},
    )


@router.post("/import")
def import_devices(
    payload: DeviceImport,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("manage_devices")),
):
    """Add devices and stations from an export file; one with the same name
    is updated. A file from 1.4 or older has no stations: each new device in
    it gets its own station, with the station settings from the file. Jobs in
    the file with a cycle time set it here; a file from 1.5 / 1.6 has cycle
    times per station instead, which go to the jobs the station has run (and
    its default job) that have none.

    All or nothing: if anything in the file is invalid, nothing is changed and
    every problem is reported. A masked password keeps the stored one.
    Stations in a file from 1.5 to 1.7 name a device per role; they get a
    source per device that starts production on any OK piece, as they counted
    before.
    """
    names = [c.name for c in payload.cameras]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise HTTPException(400, "The file lists these devices more than once: " + ", ".join(dupes))

    created, updated, errors = [], [], []
    new_devices: list[tuple[Device, dict]] = []
    for item in payload.cameras:
        data = {f: getattr(item, f) for f in DEVICE_FIELDS}
        if item.protocol not in protocols.available():
            errors.append(f"{item.name}: unknown protocol '{item.protocol}'")
            continue
        device = db.query(Device).filter(Device.name == item.name).first()
        data["protocol_config"] = _keep_secrets(data["protocol_config"], device.protocol_config if device else None)
        try:
            _normalise(data, item.protocol)
            _check_listen_port(db, item.protocol, data["port"], device.id if device else None)
        except HTTPException as exc:
            errors.append(f"{item.name}: {exc.detail}")
            continue
        if device is None:
            device = Device(**data)
            db.add(device)
            created.append(item.name)
            new_devices.append((device, {"idle_timeout_min": item.idle_timeout_min,
                                         "stats_default": item.stats_default}))
        else:
            for key, value in data.items():
                setattr(device, key, value)
            updated.append(item.name)
        db.flush()  # so later devices in the file see this one's port

    st_created, st_updated = [], []
    legacy_cycles: list[tuple[Station, float]] = []
    if payload.stations is None:
        # a file from 1.4 or older: the devices were the counted units
        taken = {n for (n,) in db.query(Station.name)}
        for device, fields in new_devices:
            st = stations.station_for_device(device, start_count=1, **fields)
            if st is not None and st.name not in taken:
                db.add(st)
                st_created.append(st.name)
    else:
        by_name = {d.name: d for d in db.query(Device)}
        st_names = [s.name for s in payload.stations]
        for n in sorted({n for n in st_names if st_names.count(n) > 1}):
            errors.append(f"station {n} is listed more than once")
        for item in payload.stations:
            missing = set()

            def device_id(name: str) -> int | None:
                dev = by_name.get(name)
                if dev is None:
                    missing.add(name)
                return dev.id if dev else None

            if isinstance(item.sources, dict):  # 1.5 - 1.7: a device per role
                sources = stations.normalize_sources(
                    {role: {"device_id": device_id(src.device), "key": src.key}
                     for role, src in item.sources.items() if role in stations.ROLES and src is not None},
                    start_count=1)
            else:
                sources = [{**src.model_dump(exclude={"device"}), "id": f"s{i}", "device_id": device_id(src.device)}
                           for i, src in enumerate(item.sources, 1)]
            for name in sorted(missing):
                errors.append(f"station {item.name}: device '{name}' is not in the file or the app")
            if missing:
                continue
            try:
                sources = stations.check_sources(db, sources)
            except ValueError as exc:
                errors.append(f"station {item.name}: {exc}")
                continue
            fields = {f: getattr(item, f) for f in STATION_FIELDS}
            st = db.query(Station).filter(Station.name == item.name).first()
            if st is None:
                st = Station(**fields, sources=sources)
                db.add(st)
                st_created.append(item.name)
            else:
                for key, value in fields.items():
                    setattr(st, key, value)
                st.sources = sources
                st_updated.append(item.name)
            if item.ideal_cycle_s and payload.jobs is None:
                legacy_cycles.append((st, item.ideal_cycle_s))

    jobs_set = []
    if payload.jobs is not None:
        job_names = [j.name for j in payload.jobs]
        for n in sorted({n for n in job_names if job_names.count(n) > 1}):
            errors.append(f"job {n} is listed more than once")
        for item in payload.jobs:
            if item.ideal_cycle_s:
                jobs.set_cycle(db, item.name.strip(), item.ideal_cycle_s)
                jobs_set.append(item.name)
    elif legacy_cycles and not errors:
        db.flush()
        epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
        picks = []
        for st, seconds in legacy_cycles:
            ran = {j: t for j, t in db.query(CounterState.job_name, CounterState.updated_at)
                   .filter(CounterState.station_id == st.id)}
            ran.setdefault(st.default_job, None)
            picks += [(jobs.aware(t) if t else epoch, st.name, j, seconds) for j, t in ran.items()]
        before = set(jobs.cycle_times(db))
        jobs.copy_station_cycles(db, picks)
        db.flush()
        jobs_set = sorted(set(jobs.cycle_times(db)) - before)

    if errors:
        db.rollback()
        raise HTTPException(400, "Nothing was imported. " + "; ".join(errors))
    db.commit()
    return {"created": created, "updated": updated, "stations_created": st_created, "stations_updated": st_updated,
            "job_cycle_times": jobs_set}


@router.get("", response_model=list[DeviceOut])
def list_devices(db: Session = Depends(get_db), _: User = Depends(require_permission("view_dashboard"))):
    return [_device_out(d) for d in db.query(Device).order_by(Device.name).all()]


@router.get("/values")
def device_values(db: Session = Depends(get_db), _: User = Depends(require_permission("view_dashboard"))):
    """For the station form: each device's values a station can use."""
    return [
        {"id": d.id, "name": d.name, "protocol": d.protocol, "connected": d.connected,
         "any_value": bool(getattr(protocols.available().get(d.protocol), "any_value", False)),
         "values": stations.value_choices(d)}
        for d in db.query(Device).order_by(Device.name).all()
    ]


@router.post("", response_model=DeviceOut, status_code=201)
def create_device(
    payload: DeviceCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("manage_devices")),
):
    if payload.protocol not in protocols.available():
        raise HTTPException(400, f"Unknown protocol: {payload.protocol}")
    if db.query(Device).filter(Device.name == payload.name).first():
        raise HTTPException(409, "A device with that name already exists")
    data = payload.model_dump(exclude={"create_station"})
    data["protocol_config"] = _keep_secrets(data["protocol_config"], None)
    _normalise(data, payload.protocol)
    _check_listen_port(db, payload.protocol, data["port"])
    device = Device(**data)
    db.add(device)
    db.flush()
    if payload.create_station:
        if db.query(Station).filter(Station.name == payload.name).first():
            db.rollback()
            raise HTTPException(409, "A station with that name already exists; add the device without one")
        st = stations.station_for_device(device)
        if st is not None:
            db.add(st)
    db.commit()
    db.refresh(device)
    return _device_out(device)


@router.patch("/{device_id}", response_model=DeviceOut)
def update_device(
    device_id: int,
    payload: DeviceUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("manage_devices")),
):
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(404, "Device not found")
    data = payload.model_dump(exclude_unset=True)
    if "protocol" in data and data["protocol"] not in protocols.available():
        raise HTTPException(400, f"Unknown protocol: {data['protocol']}")
    if "protocol_config" in data:
        data["protocol_config"] = _keep_secrets(data["protocol_config"], device.protocol_config)
        _normalise(data, data.get("protocol", device.protocol))
    _check_listen_port(db, data.get("protocol", device.protocol), data.get("port", device.port), device.id)
    for key, value in data.items():
        setattr(device, key, value)
    db.commit()
    db.refresh(device)
    return _device_out(device)


@router.delete("/{device_id}", status_code=204)
def delete_device(
    device_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("manage_devices")),
):
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(404, "Device not found")
    users = stations.stations_using(db, device_id)
    if users:
        raise HTTPException(409, "Stations use this device: " + ", ".join(s.name for s in users)
                            + ". Change or delete them first.")
    db.delete(device)
    db.commit()


@router.post("/{device_id}/poll")
def poll_now(
    device_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("control_connections")),
):
    """Read the device once, right now, and record its stations. Useful to
    test configuration."""
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(404, "Device not found")
    if protocols.is_push(device.protocol):
        raise HTTPException(
            400,
            f"This device pushes its data to the app on port {device.port}; it cannot be polled. "
            + ("It is connected." if device.connected else (device.last_error or "")),
        )
    try:
        values = read_device(db, device)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        stations.device_failed(db, device, str(exc))
        raise HTTPException(502, f"Read failed: {exc}") from exc
    readings = stations.device_read(db, device, values)
    return {
        "values": {k: v for k, v in values.items() if k != "_errors"},
        "errors": values.get("_errors") or {},
        "stations": [
            {"station_id": r.station_id, "job": r.job_name, "raw_pass": r.raw_pass, "raw_fail": r.raw_fail,
             "total_pass": r.total_pass, "total_fail": r.total_fail}
            for r in readings
        ],
    }
