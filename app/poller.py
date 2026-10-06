"""Background poller (and the listener wiring for devices that push).

Runs in a daemon thread. On each device's interval it reads the device's
values via its protocol driver (only the values its stations use, for OPC
UA), and hands them to app.stations.device_read, which updates the device and
records a reading for every station that uses it (reset-proof totals, job
changes, production state, notification rules).

One poll cycle is also exposed as ``poll_device_once`` so it can be called
synchronously from the API ("test / poll now" buttons) and from tests.

Devices on a push protocol (``tcp_listen``, ``udp_listen``, ``slmp_listen``)
are not polled; ``listener`` runs a server for each and hands every received
record to ``record_sample``, so pushed and polled data go through exactly the
same accumulation and logging.
"""
from __future__ import annotations

import logging
import threading
import time

from sqlalchemy.orm import Session

from . import notifications, protocols, stations
from .config import settings
from .counters import Sample, sample_values
from .database import SessionLocal
from .models import Device, Reading, utcnow
from .protocols.slmp_server import SlmpServerManager
from .protocols.tcp_listener import ListenerManager, ListenerRunner
from .protocols.udp_listener import UdpListenerManager

log = logging.getLogger("cognex.poller")

# seconds between checks of every station's production state (notifications)
PRODUCTION_CHECK_INTERVAL = 30


def read_device(db: Session, device: Device) -> dict:
    """Read a device's values (no recording). Raises ProtocolError."""
    driver = protocols.get_driver(
        device.protocol, device.host, device.port, {**device.protocol_config, "device_id": device.id}
    )
    return driver.read_values(stations.keys_for_device(db, device.id))


def poll_device_once(db: Session, device: Device) -> list[Reading]:
    """Read a device once and record its stations. Raises on read error."""
    return stations.device_read(db, device, read_device(db, device))


def record_sample(db: Session, device: Device, sample: Sample) -> list[Reading]:
    """A pushed record: same as a poll of the device."""
    return stations.device_read(db, device, sample_values(sample))


class Poller:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # device_id -> monotonic timestamp of next due poll
        self._next_due: dict[int, float] = {}
        self._next_production_check = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="cognex-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - never let the loop die
                pass
            self._stop.wait(0.5)

    def _tick(self) -> None:
        now = time.monotonic()
        db = SessionLocal()
        try:
            devices = db.query(Device).filter(Device.enabled.is_(True)).all()
            for device in devices:
                if protocols.is_push(device.protocol):
                    continue  # served by a listener, not polled
                interval = max(settings.min_poll_interval, device.poll_interval)
                due = self._next_due.get(device.id, 0.0)
                if now < due:
                    continue
                self._next_due[device.id] = now + interval
                self._poll_safe(db, device)
            if now >= self._next_production_check:
                # stations go idle by time, also when no reading comes in
                self._next_production_check = now + PRODUCTION_CHECK_INTERVAL
                try:
                    notifications.check_all_production(db)
                except Exception:  # noqa: BLE001
                    db.rollback()
                    log.exception("production state check failed")
        finally:
            db.close()

    def _poll_safe(self, db: Session, device: Device) -> None:
        try:
            poll_device_once(db, device)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            try:
                stations.device_failed(db, device, str(exc))
            except Exception:  # noqa: BLE001
                db.rollback()
                log.exception("could not record the failed read of device %s", device.id)


poller = Poller()


# --------------------------------------------------------------------------- #
# Listeners (devices that push data to the app: TCP, UDP, SLMP server)
# --------------------------------------------------------------------------- #


def _listener_sample(device_id: int, sample: Sample) -> None:
    db = SessionLocal()
    try:
        device = db.get(Device, device_id)
        if device is None or not device.enabled:
            return
        record_sample(db, device, sample)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _listener_status(device_id: int, connected: bool, error: str | None) -> None:
    db = SessionLocal()
    try:
        device = db.get(Device, device_id)
        if device is None:
            return
        if not connected:
            stations.device_failed(db, device, error or "disconnected")
            return
        device.connected = connected
        device.last_error = error
        device.last_poll_at = utcnow()
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("could not update listener status for device %s", device_id)
    finally:
        db.close()


def _listen_devices(protocol: str = "tcp_listen") -> list[tuple[int, str, int, dict]]:
    db = SessionLocal()
    try:
        rows = (
            db.query(Device)
            .filter(Device.enabled.is_(True), Device.protocol == protocol)
            .all()
        )
        return [(d.id, d.host, d.port, d.protocol_config or {}) for d in rows]
    finally:
        db.close()


class _Listeners:
    """Starts/stops one ListenerRunner per push protocol as a unit."""

    def __init__(self, managers: list[ListenerManager]):
        self.runners = [
            ListenerRunner(m, load=lambda p=m.protocol: _listen_devices(p)) for m in managers
        ]

    def start(self) -> None:
        for r in self.runners:
            r.start()

    def stop(self) -> None:
        for r in self.runners:
            r.stop()


listener_manager = ListenerManager(on_sample=_listener_sample, status=_listener_status)
udp_listener_manager = UdpListenerManager(on_sample=_listener_sample, status=_listener_status)
slmp_server_manager = SlmpServerManager(on_sample=_listener_sample, status=_listener_status)
listener = _Listeners([listener_manager, udp_listener_manager, slmp_server_manager])
