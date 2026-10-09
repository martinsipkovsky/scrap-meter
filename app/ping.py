"""Ping every device and keep its response time (Settings > Ping devices).

Every ``interval_s`` seconds (30 by default) each enabled device is pinged
with ICMP (icmplib; the container runs as root, so raw sockets work). Where
ICMP is not allowed, or the device does not answer it, the time to open a TCP
connection to the device's port is used instead, and the result says which
method answered. A device that pushes its data to the app (TCP / UDP listener,
SLMP server) is pinged at the address it last sent from; until it has sent
anything there is nothing to ping (shown as a dash).

Results live in memory (``result(device_id)``): the latest time in ms, the
method, and the last few results for the "Slow response" alert rule
(app.notifications.check_ping), which is checked after every round. Shown on
the Map, the Devices tab and the station view; app-wide setting kept with
settings_store (key ``ping``), on by default.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from . import settings_store
from .models import Device, utcnow

log = logging.getLogger("cognex.ping")

KEY = "ping"
DEFAULTS = {"enabled": True, "interval_s": 30}
MIN_INTERVAL, MAX_INTERVAL = 5, 3600
TIMEOUT_S = 2.0
HISTORY = 3  # results kept per device: an alert needs this many bad ones in a row
LISTENER_PROTOCOLS = {"tcp_listen", "udp_listen", "slmp_listen"}

_settings: dict | None = None
_results: dict[int, dict] = {}
_lock = threading.Lock()
_icmp_ok: bool | None = None  # None until tried; False when the container may not send ICMP


def load() -> dict:
    global _settings
    if _settings is None:
        stored = settings_store.load(KEY) or {}
        _settings = {"enabled": bool(stored.get("enabled", DEFAULTS["enabled"])),
                     "interval_s": _interval(stored.get("interval_s", DEFAULTS["interval_s"]))}
    return dict(_settings)


def _interval(value) -> int:
    try:
        return max(MIN_INTERVAL, min(MAX_INTERVAL, int(value)))
    except (TypeError, ValueError):
        return DEFAULTS["interval_s"]


def save(values: dict) -> dict:
    global _settings
    cur = load()
    new = {"enabled": bool(values.get("enabled", cur["enabled"])),
           "interval_s": _interval(values.get("interval_s", cur["interval_s"]))}
    settings_store.save(KEY, new)
    _settings = new
    if not new["enabled"]:
        with _lock:
            _results.clear()
    return dict(new)


def enabled() -> bool:
    return load()["enabled"]


def target(device: Device) -> tuple[str | None, int | None]:
    """(host, port) to ping, or (None, None) when there is nothing to ping."""
    if device.protocol in LISTENER_PROTOCOLS:
        from .protocols.tcp_listener import last_peer

        return last_peer.get(device.id), None
    if device.protocol == "opcua":
        url = (device.protocol_config or {}).get("endpoint") or ""
        try:
            u = urlparse(url)
            return (u.hostname, u.port or 4840) if u.hostname else (None, None)
        except ValueError:
            return None, None
    host = (device.host or "").strip()
    if not host or host in ("0.0.0.0", "*", "any") or device.protocol == "simulator":
        return None, None
    return host, device.port or None


def _icmp(host: str) -> float | None:
    """Round trip in ms, None when no reply. Raises PermissionError when the
    container may not send ICMP."""
    from icmplib import ping as icmp_ping
    from icmplib.exceptions import SocketPermissionError

    try:
        r = icmp_ping(host, count=1, timeout=TIMEOUT_S, privileged=True)
    except SocketPermissionError:
        try:
            r = icmp_ping(host, count=1, timeout=TIMEOUT_S, privileged=False)
        except SocketPermissionError as exc:
            raise PermissionError(str(exc)) from exc
    return r.avg_rtt if r.is_alive else None


def _tcp(host: str, port: int) -> float | None:
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT_S):
            return (time.perf_counter() - t0) * 1000
    except ConnectionRefusedError:
        # the host answered (with a refusal): it is reachable
        return (time.perf_counter() - t0) * 1000
    except OSError:
        return None


def measure(host: str, port: int | None) -> tuple[float | None, str]:
    """(ms or None, method) for one host: ICMP, else the TCP connect time."""
    global _icmp_ok
    if _icmp_ok is not False:
        try:
            ms = _icmp(host)
            _icmp_ok = True
            if ms is not None or not port:
                return ms, "icmp"
        except PermissionError:
            _icmp_ok = False
            log.warning("ICMP ping is not allowed here; using the TCP connect time instead")
        except Exception as exc:  # noqa: BLE001 - a bad name and so on: try TCP
            log.debug("icmp ping of %s failed: %s", host, exc)
    if port:
        return _tcp(host, port), "tcp"
    return None, "icmp"


def ping_device(device: Device) -> dict:
    host, port = target(device)
    if not host:
        res = {"ms": None, "method": None, "target": None, "ok": None, "at": utcnow()}
    else:
        ms, method = measure(host, port)
        res = {"ms": round(ms, 1) if ms is not None else None, "method": method,
               "target": f"{host}:{port}" if method == "tcp" else host, "ok": ms is not None, "at": utcnow()}
    with _lock:
        prev = _results.get(device.id)
        recent = deque(prev["recent"] if prev else [], maxlen=HISTORY)
        if res["ok"] is not None:
            recent.append(res["ms"])  # None = no reply
        res["recent"] = list(recent)
        _results[device.id] = res
    return res


def result(device_id: int) -> dict | None:
    """The latest result for the API (without the history), None when off or
    not pinged yet."""
    if not enabled():
        return None
    with _lock:
        r = _results.get(device_id)
    return {k: v for k, v in r.items() if k != "recent"} if r else None


def recent(device_id: int) -> list:
    with _lock:
        r = _results.get(device_id)
    return list(r["recent"]) if r else []


def run_round() -> None:
    """Ping every enabled device (in parallel), then check the alert rules."""
    from . import notifications
    from .database import SessionLocal

    db = SessionLocal()
    try:
        devices = db.query(Device).filter(Device.enabled.is_(True)).all()
        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(ping_device, devices))
        with _lock:
            for did in [d for d in _results if d not in {x.id for x in devices}]:
                del _results[did]
        notifications.check_ping(db)
    finally:
        db.close()


class Pinger:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="pinger", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        delay = 5
        while not self._stop.wait(delay):
            settings = load()
            delay = settings["interval_s"]
            if not settings["enabled"]:
                continue
            try:
                run_round()
            except Exception:  # noqa: BLE001 - never stop pinging
                log.exception("ping round failed")


pinger = Pinger()

