"""System tab (administrators): how the computer and the app are doing.

``snapshot`` gathers the live values: CPU, memory, disks (the container's
root, the data volume at DATA_DIR) and the database size, network interfaces
with their traffic, uptime, versions, the database connection, the listening
ports and the poller (devices read, read errors). Values are what the app
sees from inside its container: in Docker, CPU and memory are the host's, the
network is the container's own interfaces.

``sampler`` (only with POLL_ENABLED, like the other background services)
stores one row in system_samples every SAMPLE_S seconds for the graphs and
deletes rows older than KEEP_DAYS.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import platform
import socket
import sys
import threading
import time
from pathlib import Path

import psutil
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from . import __version__
from .config import settings
from .models import Device, SystemSample, utcnow

log = logging.getLogger("cognex.system")

SAMPLE_S = 60
KEEP_DAYS = 7
_STARTED = time.time()
# the previous network counters, for the traffic rate: (monotonic time, {nic: (rx, tx)})
_last_net: tuple[float, dict[str, tuple[int, int]]] | None = None
_net_lock = threading.Lock()


def _disk(path: str) -> dict | None:
    try:
        u = psutil.disk_usage(path)
    except OSError:
        return None
    return {"path": path, "total": u.total, "used": u.used, "free": u.free, "percent": u.percent}


def _db_info(db: Session) -> dict:
    bind = db.get_bind()
    info = {"dialect": bind.dialect.name, "connected": False, "size_bytes": None, "server": None,
            "latency_ms": None, "error": None}
    try:
        t0 = time.perf_counter()
        db.execute(text("SELECT 1"))
        info["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        info["connected"] = True
        if bind.dialect.name == "postgresql":
            info["size_bytes"] = db.execute(text("SELECT pg_database_size(current_database())")).scalar()
            info["server"] = "PostgreSQL " + str(db.execute(text("SHOW server_version")).scalar())
        elif bind.dialect.name == "sqlite":
            path = bind.url.database
            info["size_bytes"] = os.path.getsize(path) if path and os.path.exists(path) else None
            info["server"] = "SQLite " + db.execute(text("SELECT sqlite_version()")).scalar()
    except Exception as exc:  # noqa: BLE001 - shown on the page
        db.rollback()
        info["error"] = str(exc).splitlines()[0][:200]
    return info


def _network() -> tuple[list[dict], float, float]:
    """Interfaces with addresses, totals and rates; the total rx/tx rates
    (bytes per second, loopback left out)."""
    global _last_net
    counters = psutil.net_io_counters(pernic=True)
    addrs = psutil.net_if_addrs()
    stats = psutil.net_if_stats()
    now = time.monotonic()
    with _net_lock:
        prev = _last_net
        _last_net = (now, {n: (c.bytes_recv, c.bytes_sent) for n, c in counters.items()})
    nics, rx_total, tx_total = [], 0.0, 0.0
    for name, c in sorted(counters.items()):
        rx_rate = tx_rate = None
        if prev and name in prev[1] and now > prev[0]:
            span = now - prev[0]
            rx_rate = max(0.0, (c.bytes_recv - prev[1][name][0]) / span)
            tx_rate = max(0.0, (c.bytes_sent - prev[1][name][1]) / span)
            if name != "lo":
                rx_total += rx_rate
                tx_total += tx_rate
        st = stats.get(name)
        nics.append({
            "name": name,
            "up": bool(st and st.isup),
            "speed_mbps": st.speed if st else None,
            "addresses": [a.address for a in addrs.get(name, []) if a.family in (socket.AF_INET, socket.AF_INET6)],
            "rx_bytes": c.bytes_recv, "tx_bytes": c.bytes_sent,
            "rx_rate": rx_rate, "tx_rate": tx_rate,
            "errors": c.errin + c.errout, "drops": c.dropin + c.dropout,
        })
    return nics, rx_total, tx_total


def _listeners(db: Session) -> list[dict]:
    from .poller import listener_manager, slmp_server_manager, udp_listener_manager
    from . import powerbi_access

    out = []
    for kind, mgr in (("TCP", listener_manager), ("UDP", udp_listener_manager), ("SLMP", slmp_server_manager)):
        try:
            for device_id, port in mgr.ports().items():
                out.append({"kind": kind, "port": port, "device_id": device_id})
        except Exception:  # noqa: BLE001
            continue
    fwd = powerbi_access.forwarder
    if fwd.running:
        out.append({"kind": "Power BI", "port": fwd.port, "device_id": None})
    names = dict(db.query(Device.id, Device.name).all())
    for p in out:
        p["device_name"] = names.get(p["device_id"])
    return sorted(out, key=lambda p: p["port"] or 0)


def _poller(db: Session) -> dict:
    from .poller import poller

    devices = db.query(Device).all()
    enabled = [d for d in devices if d.enabled]
    last = max((d.last_poll_at for d in enabled if d.last_poll_at), default=None)
    return {
        "running": bool(poller._thread and poller._thread.is_alive()),
        "poll_enabled": settings.poll_enabled,
        "devices": len(devices),
        "enabled": len(enabled),
        "connected": sum(1 for d in enabled if d.connected),
        "read_errors": sum(1 for d in enabled if not d.connected and d.last_error),
        "errors": [{"id": d.id, "name": d.name, "error": d.last_error} for d in enabled
                   if not d.connected and d.last_error][:20],
        "last_read_at": last,
    }


def snapshot(db: Session) -> dict:
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    try:
        load = os.getloadavg()
    except (OSError, AttributeError):
        load = None
    nics, rx, tx = _network()
    proc = psutil.Process()
    with proc.oneshot():
        app_mem = proc.memory_info().rss
        threads = proc.num_threads()
    disks = [d for d in (_disk("/"), _disk(settings.data_dir)) if d]
    if len(disks) == 2 and disks[0]["total"] == disks[1]["total"] and disks[0]["used"] == disks[1]["used"]:
        disks[1]["same_as_root"] = True
    return {
        "time": utcnow(),
        "host": {
            "hostname": socket.gethostname(),
            "os": f"{platform.system()} {platform.release()}",
            "distro": _distro(),
            "machine": platform.machine(),
            "python": sys.version.split()[0],
            "app_version": __version__,
            "boot_time": dt.datetime.fromtimestamp(psutil.boot_time(), dt.timezone.utc),
            "app_started": dt.datetime.fromtimestamp(_STARTED, dt.timezone.utc),
            "in_container": Path("/.dockerenv").exists(),
        },
        "cpu": {"percent": psutil.cpu_percent(interval=None), "cores": psutil.cpu_count() or 0,
                "load": [round(x, 2) for x in load] if load else None},
        "memory": {"total": vm.total, "used": vm.total - vm.available, "percent": vm.percent,
                   "swap_total": sw.total, "swap_used": sw.used, "app_rss": app_mem, "app_threads": threads},
        "disks": disks,
        "database": _db_info(db),
        "network": {"interfaces": nics, "rx_rate": rx, "tx_rate": tx},
        "listeners": _listeners(db),
        "listen_ports": settings.listen_ports,
        "poller": _poller(db),
    }


def _distro() -> str | None:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip('"')
    except OSError:
        pass
    return None


def take_sample(db: Session) -> SystemSample:
    snap = snapshot(db)
    data_disk = next((d for d in snap["disks"] if d["path"] != "/"), None)
    row = SystemSample(
        created_at=snap["time"],
        cpu_percent=snap["cpu"]["percent"],
        load1=snap["cpu"]["load"][0] if snap["cpu"]["load"] else None,
        mem_percent=snap["memory"]["percent"],
        app_rss=snap["memory"]["app_rss"],
        disk_percent=snap["disks"][0]["percent"] if snap["disks"] else None,
        data_disk_percent=data_disk["percent"] if data_disk else None,
        db_bytes=snap["database"]["size_bytes"],
        net_rx=snap["network"]["rx_rate"],
        net_tx=snap["network"]["tx_rate"],
        devices_ok=snap["poller"]["connected"],
        read_errors=snap["poller"]["read_errors"],
    )
    db.add(row)
    db.query(SystemSample).filter(SystemSample.created_at < utcnow() - dt.timedelta(days=KEEP_DAYS)).delete(
        synchronize_session=False)
    db.commit()
    return row


SERIES = ("cpu_percent", "load1", "mem_percent", "app_rss", "disk_percent", "data_disk_percent", "db_bytes",
          "net_rx", "net_tx", "devices_ok", "read_errors")


def history(db: Session, hours: float) -> dict:
    hours = max(1.0, min(float(hours), KEEP_DAYS * 24))
    since = utcnow() - dt.timedelta(hours=hours)
    rows = db.query(SystemSample).filter(SystemSample.created_at >= since).order_by(SystemSample.created_at).all()
    # at most ~600 points per graph: average buckets of rows
    step = max(1, len(rows) // 600)
    points = []
    for i in range(0, len(rows), step):
        chunk = rows[i:i + step]
        point = {"t": chunk[-1].created_at}
        for key in SERIES:
            vals = [getattr(r, key) for r in chunk if getattr(r, key) is not None]
            point[key] = sum(vals) / len(vals) if vals else None
        points.append(point)
    return {"hours": hours, "sample_s": SAMPLE_S, "points": points,
            "count": db.query(func.count(SystemSample.id)).scalar()}


class Sampler:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        psutil.cpu_percent(interval=None)  # the first call only starts the CPU measurement
        self._thread = threading.Thread(target=self._run, name="system-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        from .database import SessionLocal

        delay = 5  # the first sample soon, so the graphs start right after a restart
        while not self._stop.wait(delay):
            delay = SAMPLE_S
            db = SessionLocal()
            try:
                take_sample(db)
            except Exception:  # noqa: BLE001 - never stop sampling
                db.rollback()
                log.exception("could not store a system sample")
            finally:
                db.close()


sampler = Sampler()
