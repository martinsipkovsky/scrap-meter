"""Device CRUD, protocol metadata, manual poll/test, production start/stop,
export/import of device configurations, and the OPC UA Browse helper.

Passwords in a protocol config (e.g. an OPC UA login) never leave the server:
the API and export files show SECRET_MASK instead, and saving SECRET_MASK
keeps the stored password."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import production, protocols
from ..config import settings
from ..database import get_db
from ..dependencies import require_api_user, require_permission
from ..models import CounterState, Device, User
from ..poller import poll_device_once
from ..protocols import opcua
from ..protocols.base import ProtocolError
from ..protocols.tcp_listener import parse_port_range
from ..schemas import DeviceCreate, DeviceExportItem, DeviceImport, DeviceOut, DeviceUpdate

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
    """Port range published for cameras that push data to the app."""
    lo, hi = parse_port_range(settings.listen_ports)
    return {"first": lo, "last": hi}


def _check_listen_port(db: Session, protocol: str, port: int, device_id: int | None = None) -> None:
    """A pushing camera needs a free port inside the published range."""
    if not protocols.is_push(protocol):
        return
    lo, hi = parse_port_range(settings.listen_ports)
    if not lo <= port <= hi:
        raise HTTPException(
            400,
            f"Listen port must be between {lo} and {hi} (the ports published in "
            f"docker-compose, LISTEN_PORTS). Got {port}.",
        )
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


EXPORT_FIELDS = tuple(DeviceExportItem.model_fields)


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
    """List a node's children on an OPC UA server, to pick counter node ids."""
    device = db.get(Device, payload.device_id) if payload.device_id else None
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
    """All device configurations as a downloadable JSON file (no counter
    history, no passwords). The list keeps its old key "cameras" so files
    move between old and new versions."""
    cameras = [
        {**{f: getattr(d, f) for f in EXPORT_FIELDS}, "protocol_config": masked_config(d.protocol_config)}
        for d in db.query(Device).order_by(Device.name).all()
    ]
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    return JSONResponse(
        {"version": 1, "exported_at": dt.datetime.now(dt.timezone.utc).isoformat(), "cameras": cameras},
        headers={"Content-Disposition": f'attachment; filename="scrap-meter-devices-{stamp}.json"'},
    )


@router.post("/import")
def import_devices(
    payload: DeviceImport,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("manage_devices")),
):
    """Add devices from an export file; a device with the same name is updated.

    All or nothing: if any device in the file is invalid, nothing is changed and
    every problem is reported. A masked password keeps the stored one.
    """
    names = [c.name for c in payload.cameras]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise HTTPException(400, "The file lists these devices more than once: " + ", ".join(dupes))

    created, updated, errors = [], [], []
    for item in payload.cameras:
        data = item.model_dump()
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
            db.add(Device(**data))
            created.append(item.name)
        else:
            for key, value in data.items():
                setattr(device, key, value)
            updated.append(item.name)
        db.flush()  # so later devices in the file see this one's port

    if errors:
        db.rollback()
        raise HTTPException(400, "Nothing was imported. " + "; ".join(errors))
    db.commit()
    return {"created": created, "updated": updated}


@router.get("", response_model=list[DeviceOut])
def list_devices(db: Session = Depends(get_db), _: User = Depends(require_permission("view_dashboard"))):
    return [_device_out(d) for d in db.query(Device).order_by(Device.name).all()]


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
    data = payload.model_dump()
    data["protocol_config"] = _keep_secrets(data["protocol_config"], None)
    _normalise(data, payload.protocol)
    _check_listen_port(db, payload.protocol, data["port"])
    device = Device(**data)
    db.add(device)
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
    db.delete(device)
    db.commit()


@router.post("/{device_id}/poll")
def poll_now(
    device_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("control_connections")),
):
    """Read the device once, right now. Useful to test configuration."""
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
        reading = poll_device_once(db, device)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        device.connected = False
        device.last_error = str(exc)
        db.commit()
        raise HTTPException(502, f"Read failed: {exc}") from exc
    return {
        "job": reading.job_name,
        "raw_pass": reading.raw_pass,
        "raw_fail": reading.raw_fail,
        "total_pass": reading.total_pass,
        "total_fail": reading.total_fail,
        "extra": reading.extra,
    }


def _set_production(db: Session, device_id: int, running: bool) -> dict:
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(404, "Device not found")
    if running:
        production.start(device)
    else:
        production.stop(device)
    db.commit()
    return production.describe(device)


@router.post("/{device_id}/production/start")
def production_start(
    device_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("control_connections")),
):
    """Put the device back in production now (clears a manual stop)."""
    return _set_production(db, device_id, True)


@router.post("/{device_id}/production/stop")
def production_stop(
    device_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("control_connections")),
):
    """Mark the device as not in production until Start is pressed."""
    return _set_production(db, device_id, False)


@router.post("/{device_id}/counters/reset")
def reset_counters(
    device_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("control_connections")),
):
    """Start the counters shown on the dashboard from zero for the current job.

    Nothing is sent to the device, and the job totals in the readings history
    and the scrap statistics stay as they are (see CounterState.reset_shown).
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(404, "Device not found")
    state = (
        db.query(CounterState)
        .filter(CounterState.device_id == device_id, CounterState.is_active.is_(True))
        .first()
    )
    if state is None:
        raise HTTPException(400, "This device has no counters yet")
    state.reset_shown()
    db.commit()
    return {"job_name": state.job_name, "reset_at": state.reset_at}


@router.get("/{device_id}/counters")
def device_counters(
    device_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("view_data")),
):
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(404, "Device not found")
    states = (
        db.query(CounterState)
        .filter(CounterState.device_id == device_id)
        .order_by(CounterState.is_active.desc(), CounterState.updated_at.desc())
        .all()
    )
    return [
        {
            "job_name": s.job_name,
            "total_pass": s.total_pass,
            "total_fail": s.total_fail,
            "total_count": s.total_count,
            "scrap_rate": round(s.scrap_rate, 4),
            "is_active": s.is_active,
            "updated_at": s.updated_at,
        }
        for s in states
    ]
