"""TCP listener (camera pushes to the app) driver.

Every other protocol has the app connect *to* the camera. This one turns it
around: the app opens a TCP server on the camera's configured port and the
camera (In-Sight "TCP/IP client" / Data Channel, or any device that can send a
line of text) connects *to the app* and pushes one ASCII record per
inspection. Each record is parsed into a ``counters.Sample`` and fed through
the same accumulation, logging and notifications as a polled camera.

On the Cameras tab:

* **Port** is the port the app listens on. It must be inside the range that
  docker-compose publishes (``LISTEN_PORTS``, default 5100-5119), and each
  listening camera needs its own port.
* **Host** is optional: the camera's IP to accept data from. Leave it blank
  (or ``0.0.0.0``) to accept any sender.

Record layout is configurable, same idea as the Data Channel driver::

    config = {
        "delimiter": ",",          # field separator in the record
        "terminator": "\\r\\n",     # record terminator ("\\n" also accepted)
        "mode": "counter",         # "counter": record carries running totals
                                   # "event":   one record per part (Pass/Fail)
        "job_field": 0,            # index of the job name (null = use default_job)
        "default_job": "MAIN",     # job name when the record has none
        "pass_field": 1,           # counter mode: index of the pass counter
        "fail_field": 2,           # counter mode: index of the fail counter
        "count_field": null,       # counter mode: optional total-count index
        "status_field": 1,         # event mode: index of the Pass/Fail status
        "pass_values": ["pass", "ok", "1", "good", "p"]   # event mode
    }

In event mode the listener keeps its own running pass/fail tally per job and
hands those to the accumulator, so every pushed part is counted. The tally
lives in memory; after a container restart it starts again from zero, which
the accumulator treats as a counter reset, so no banked totals are lost.

This file holds both the parser (``TcpListenerDriver``) and the socket server
(``ListenerManager``). The manager is started from app.main and keeps one
server per enabled listening camera, re-syncing whenever cameras change.
"""
from __future__ import annotations

import logging
import socket
import socketserver
import threading
import time
from typing import Callable

from ..counters import Sample
from .base import ProtocolDriver, ProtocolError

log = logging.getLogger("cognex.tcp_listener")

ANY_HOST = ("", "0.0.0.0", "*", "any")
MAX_RECORD = 8192


def _to_int(value: str) -> int:
    value = (value or "").strip()
    if not value:
        return 0
    try:
        return int(float(value))
    except ValueError:
        return 0


def _field(fields: list[str], index, default: str = "") -> str:
    if index is None or index == "":
        return default
    try:
        return fields[int(index)]
    except (IndexError, ValueError, TypeError):
        return default


def parse_port_range(spec: str) -> tuple[int, int]:
    """'5100-5119' -> (5100, 5119); '5100' -> (5100, 5100)."""
    spec = (spec or "").strip()
    lo, _, hi = spec.partition("-")
    lo_i = int(lo)
    hi_i = int(hi) if hi else lo_i
    return (min(lo_i, hi_i), max(lo_i, hi_i))


class TcpListenerDriver(ProtocolDriver):
    key = "tcp_listen"
    label = "TCP listener (device pushes data)"
    #: tells the poller not to poll this device; the ListenerManager serves it
    push = True
    #: shown on the Devices form when this protocol is picked
    push_help = ("The app opens a TCP server on this port and the device connects to it "
                 "(In-Sight: TCP/IP client pointing at this server's IP and the port below). "
                 "Each line the device sends is split by the delimiter; set which field holds "
                 "the job name and the counters below.")
    config_fields = {
        "delimiter": "Field separator in each record (default ',')",
        "terminator": "Record terminator (default CRLF; a bare LF also works)",
        "mode": "'counter' (record carries running totals) or 'event' (one record per part)",
        "job_field": "Zero-based index of the job name (null = always use default_job)",
        "default_job": "Job name when the record has none (default 'MAIN')",
        "pass_field": "Counter mode: index of the pass counter (default 1)",
        "fail_field": "Counter mode: index of the fail counter (default 2)",
        "count_field": "Counter mode: optional index of a total-count field",
        "status_field": "Event mode: index of the Pass/Fail status (default 1)",
        "pass_values": "Event mode: status values meaning pass (default pass/ok/1/good/p)",
    }

    def __init__(self, host: str, port: int, config: dict | None = None):
        super().__init__(host, port, config)
        # event-mode running tally: job -> [pass, fail]
        self._events: dict[str, list[int]] = {}
        self._last_job: str | None = None

    def read(self) -> Sample:
        raise ProtocolError(
            "This device pushes its data to the app (TCP listener); it is not polled. "
            "Point the camera at this server's IP on port %s." % self.port
        )

    # ---- parsing ---------------------------------------------------------
    @property
    def terminator(self) -> bytes:
        term = self.config.get("terminator", "\r\n") or "\r\n"
        return term.encode().decode("unicode_escape").encode("latin-1")

    def split_records(self, buf: bytearray) -> list[str]:
        """Pop every complete record off ``buf`` (mutates it)."""
        term = self.terminator
        # CRLF: split on LF and strip the CR, so a camera that ends records
        # with a bare LF works too
        if term == b"\r\n":
            term = b"\n"
        out: list[str] = []
        while True:
            idx = buf.find(term)
            if idx == -1:
                break
            raw = bytes(buf[:idx])
            del buf[: idx + len(term)]
            line = raw.decode("ascii", errors="replace").strip()
            if line:
                out.append(line)
        if len(buf) > MAX_RECORD:  # no terminator ever arrived; drop garbage
            buf.clear()
        return out

    def parse(self, line: str) -> Sample:
        cfg = self.config
        delimiter = cfg.get("delimiter", ",") or ","
        fields = [f.strip() for f in line.split(delimiter)]

        job = _field(fields, cfg.get("job_field", 0)) or cfg.get("default_job") or "MAIN"
        mode = cfg.get("mode", "counter")

        if mode == "event":
            status = _field(fields, cfg.get("status_field", 1)).lower()
            pass_values = [str(v).lower() for v in cfg.get("pass_values") or
                           ("pass", "ok", "1", "good", "p")]
            is_pass = status in pass_values
            if job != self._last_job:
                # new job: start its tally from zero so the accumulator's
                # baseline for the new job is exactly this part
                self._events[job] = [0, 0]
                self._last_job = job
            tally = self._events.setdefault(job, [0, 0])
            tally[0 if is_pass else 1] += 1
            return Sample(
                job_name=job,
                raw_pass=tally[0],
                raw_fail=tally[1],
                extra={"raw": line, "mode": "event", "status": status},
            )

        raw_count = 0
        if cfg.get("count_field") not in (None, ""):
            raw_count = _to_int(_field(fields, cfg.get("count_field")))
        return Sample(
            job_name=job,
            raw_pass=_to_int(_field(fields, cfg.get("pass_field", 1))),
            raw_fail=_to_int(_field(fields, cfg.get("fail_field", 2))),
            raw_count=raw_count,
            extra={"raw": line},
        )


# --------------------------------------------------------------------------- #
# Socket server
# --------------------------------------------------------------------------- #

#: (device_id, sample) -> None. Persists the sample; set by the manager owner.
SampleHandler = Callable[[int, Sample], None]
#: (device_id, connected, error) -> None. Updates the device's online status.
StatusHandler = Callable[[int, bool, "str | None"], None]


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port: int, device_id: int, driver: TcpListenerDriver,
                 allowed_host: str, manager: "ListenerManager"):
        self.device_id = device_id
        self.driver = driver
        self.allowed_host = allowed_host
        self.manager = manager
        self.clients = 0
        self.lock = threading.Lock()
        super().__init__(("0.0.0.0", port), _Handler)


class _Handler(socketserver.BaseRequestHandler):
    server: _Server

    def handle(self) -> None:
        srv = self.server
        peer = self.client_address[0]
        if srv.allowed_host not in ANY_HOST and peer != srv.allowed_host:
            log.warning("tcp_listen port %s: rejected connection from %s (allowed %s)",
                        srv.server_address[1], peer, srv.allowed_host)
            return
        with srv.lock:
            srv.clients += 1
        srv.manager.status(srv.device_id, True, None)
        self.request.settimeout(None)
        buf = bytearray()
        try:
            while True:
                chunk = self.request.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                for line in srv.driver.split_records(buf):
                    self._deliver(line)
            # a final record without a terminator, sent just before closing
            tail = bytes(buf).decode("ascii", errors="replace").strip()
            if tail:
                self._deliver(tail)
        except OSError as exc:
            log.info("tcp_listen port %s: connection from %s ended: %s",
                     srv.server_address[1], peer, exc)
        finally:
            with srv.lock:
                srv.clients -= 1
                still = srv.clients > 0
            if not still:
                srv.manager.status(srv.device_id, False, f"Camera {peer} disconnected")

    def _deliver(self, line: str) -> None:
        srv = self.server
        try:
            with srv.manager.lock_for(srv.device_id):
                sample = srv.driver.parse(line)
                srv.manager.on_sample(srv.device_id, sample)
        except Exception as exc:  # noqa: BLE001 - one bad record must not drop the link
            log.warning("tcp_listen device %s: could not process %r: %s", srv.device_id, line, exc)


class ListenerManager:
    """Keeps one TCP server per enabled listening camera.

    ``sync(devices)`` is given ``(device_id, host, port, config)`` tuples for
    every enabled ``tcp_listen`` camera and starts/stops/restarts servers to
    match. Errors (e.g. port already in use) are reported through ``status``.

    Other push protocols (UDP listener, SLMP server) reuse this class and only
    override ``protocol``, ``waiting`` and ``make_server``.
    """

    #: Device.protocol this manager serves
    protocol = "tcp_listen"
    #: status text shown until the camera first sends something
    waiting = "Listening on TCP port {port}, waiting for the camera to connect"

    def __init__(self, on_sample: SampleHandler, status: StatusHandler):
        self.on_sample = on_sample
        self.status = status
        self._servers: dict[int, tuple[tuple, _Server]] = {}
        self._failed: dict[int, tuple] = {}
        self._locks: dict[int, threading.Lock] = {}
        self._mutex = threading.Lock()

    def lock_for(self, device_id: int) -> threading.Lock:
        with self._mutex:
            return self._locks.setdefault(device_id, threading.Lock())

    def ports(self) -> dict[int, int]:
        """device_id -> listening port, for running servers."""
        return {did: srv.server_address[1] for did, (_, srv) in self._servers.items()}

    def sync(self, devices: list[tuple[int, str, int, dict]]) -> None:
        wanted = {d[0]: (d[1] or "", int(d[2]), dict(d[3] or {})) for d in devices}
        with self._mutex:
            # stop servers that are gone or whose settings changed
            for did in list(self._servers):
                sig, srv = self._servers[did]
                if wanted.get(did) != sig:
                    self._stop(srv)
                    del self._servers[did]
            for did in list(self._failed):
                if wanted.get(did) != self._failed[did]:
                    del self._failed[did]
            # start missing ones
            for did, sig in wanted.items():
                if did in self._servers or self._failed.get(did) == sig:
                    continue
                host, port, cfg = sig
                try:
                    srv = self.make_server(did, host, port, cfg)
                except OSError as exc:
                    self._failed[did] = sig
                    self.status(did, False, f"Cannot listen on port {port}: {exc}")
                    continue
                threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.5},
                                 name=f"{self.protocol}-{port}", daemon=True).start()
                self._servers[did] = (sig, srv)
                log.info("%s: device %s listening on port %s", self.protocol, did, port)
                self.status(did, False, self.waiting.format(port=port))

    def make_server(self, device_id: int, host: str, port: int, cfg: dict):
        """Bind the server for one camera (raises OSError if the port is taken).

        The returned object needs ``serve_forever``, ``shutdown``,
        ``server_close`` and ``server_address``, i.e. any socketserver server.
        """
        return _Server(port, device_id, TcpListenerDriver(host, port, cfg), host.strip(), self)

    def retry_failed(self) -> None:
        """Forget failed binds so the next sync tries them again."""
        with self._mutex:
            self._failed.clear()

    def stop_all(self) -> None:
        with self._mutex:
            for _, srv in self._servers.values():
                self._stop(srv)
            self._servers.clear()

    @staticmethod
    def _stop(srv) -> None:
        srv.shutdown()
        srv.server_close()


class ListenerRunner:
    """Background thread that keeps a ListenerManager in sync with the DB."""

    def __init__(self, manager: ListenerManager, load: Callable[[], list], interval: float = 2.0):
        self.manager = manager
        self.load = load
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"{self.manager.protocol}-sync",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.manager.stop_all()

    def _run(self) -> None:
        last_retry = time.monotonic()
        while not self._stop.is_set():
            try:
                if time.monotonic() - last_retry > 30:  # e.g. port freed up
                    self.manager.retry_failed()
                    last_retry = time.monotonic()
                self.manager.sync(self.load())
            except Exception:  # noqa: BLE001 - never let the loop die
                log.exception("%s sync failed", self.manager.protocol)
            self._stop.wait(self.interval)
