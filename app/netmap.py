"""Map tab: the Scrap Meter server, the devices it talks to and the stations
that count them, with live status and ping times.

``graph`` returns the nodes and links; positions dragged on the Map are kept
for everyone with settings_store (key ``map_layout``: {"server": [x, y],
"d<id>": [x, y], "s<id>": [x, y]}). A node without a saved position gets one
from the page's automatic layout.
"""
from __future__ import annotations

import socket

from sqlalchemy.orm import Session

from . import __version__, ping, production, settings_store, stations
from .config import settings
from .models import Device, Station

KEY = "map_layout"
LISTEN_KIND = {"tcp_listen": "TCP", "udp_listen": "UDP", "slmp_listen": "SLMP"}


def device_status(device: Device) -> dict:
    """The device's own state with the dashboard's problem codes."""
    if not device.enabled:
        return {"state": "off", "code": "W03", "message": f"Device '{device.name}' is disabled"}
    if not device.connected and device.last_error is None and device.last_poll_at is None:
        return {"state": "waiting", "code": "W01", "message": "waiting for the first read"}
    if not device.connected:
        error = device.last_error or "offline"
        return {"state": "error", "code": stations.problem_code(error), "message": error}
    return {"state": "ok", "code": None, "message": None}


def _address(d: Device) -> str:
    if d.protocol in LISTEN_KIND:
        return f"listens on {LISTEN_KIND[d.protocol]} :{d.port}"
    if d.protocol == "opcua":
        return (d.protocol_config or {}).get("endpoint") or f"{d.host}:{d.port}"
    if d.protocol == "simulator":
        return "simulated"
    return f"{d.host}:{d.port}"


def load_layout() -> dict:
    return settings_store.load(KEY) or {}


def save_layout(positions: dict) -> dict:
    """Merge dragged positions; a null position forgets the node's place."""
    layout = load_layout()
    for key, pos in positions.items():
        if not (key == "server" or (key[:1] in "ds" and key[1:].isdigit())):
            continue
        if pos is None:
            layout.pop(key, None)
        elif isinstance(pos, (list, tuple)) and len(pos) == 2:
            layout[key] = [round(float(pos[0]), 1), round(float(pos[1]), 1)]
    settings_store.save(KEY, layout)
    return layout


def graph(db: Session) -> dict:
    devices = db.query(Device).order_by(Device.name).all()
    by_id = {d.id: d for d in devices}
    sts = db.query(Station).order_by(Station.sort_order, Station.name).all()
    layout = load_layout()
    from .protocols.tcp_listener import last_peer

    dev_out = []
    for d in devices:
        listen = LISTEN_KIND.get(d.protocol)
        dev_out.append({
            "id": d.id, "name": d.name, "protocol": d.protocol, "address": _address(d),
            "host": d.host, "port": d.port, "enabled": d.enabled, "connected": d.connected,
            "listen": {"kind": listen, "port": d.port, "peer": last_peer.get(d.id)} if listen else None,
            "status": device_status(d), "ping": ping.result(d.id),
            "pos": layout.get(f"d{d.id}"),
        })
    st_out, links = [], []
    for s in sts:
        online = stations.status(s, {i: by_id[i] for i in s.device_ids() if i in by_id})
        first = online["problems"][0] if online["problems"] else None
        st_out.append({
            "id": s.id, "name": s.name, "production_state": production.state(s), "current_job": s.current_job,
            "connected": online["connected"], "code": first["code"] if first else None,
            "problem": online["problem"], "alerts_muted": bool(s.alerts_muted),
            "pos": layout.get(f"s{s.id}"),
        })
        for device_id in sorted(s.device_ids()):
            if device_id in by_id:
                links.append({"station_id": s.id, "device_id": device_id})
    return {
        "server": {"name": "Scrap Meter", "host": socket.gethostname(), "version": __version__,
                   "listen_ports": settings.listen_ports, "pos": layout.get("server")},
        "devices": dev_out, "stations": st_out, "links": links,
        "ping": ping.load(),
    }
