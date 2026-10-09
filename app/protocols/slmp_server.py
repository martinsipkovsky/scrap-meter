"""SLMP server (the app stands in for a Mitsubishi PLC) driver.

Cognex In-Sight cameras speak SLMP as a *client*: with "SLMP Protocol" /
"SLMP Scanner" enabled they connect to a PLC and write their results into its
device memory (D/W registers). When there is no PLC, or you want the camera to
talk to this app directly, pick this protocol: the app opens an SLMP server
(3E binary frame over TCP) on the camera's port, and the camera is pointed at
this server's IP and that port as if the app were the PLC.

The app keeps an in-memory copy of the "PLC" registers for each camera,
answers the camera's reads (registers it never wrote read as 0, or the
``preset`` values) and, after every write, takes the job and counters from the
registers named in the config. If there *is* a PLC in between, use the
``slmp`` driver instead and read the PLC.

On the Cameras tab:

* **Port** is the TCP port the app listens on, inside ``LISTEN_PORTS``
  (default 5100-5119). Each listening camera needs its own port.
* **Host** is optional: the camera's IP to accept connections from.

Config::

    config = {
        "mode": "counter",         # "counter": the camera writes running totals
                                   # "event":   the camera writes one result per part
        "pass_device": "D100",     # counter mode: pass counter register
        "fail_device": "D102",     # counter mode: fail counter register
        "count_device": null,      # counter mode: optional total counter
        "width": 1,                # 1 = 16-bit, 2 = 32-bit (two words, low word first)
        "trigger_device": "D110",  # event mode: register that changes once per part
                                   #   (e.g. the inspection / result ID)
        "status_device": "D111",   # event mode: register holding the result
        "pass_values": [1],        # event mode: status values that mean pass
        "job_device": null,        # optional first register of the job name / number
        "job_length": 8,           # words of the ASCII job name (2 chars each)
        "job_format": "ascii",     # "ascii" or "number"
        "default_job": "MAIN",
        "preset": {"D0": 1}        # optional values the camera reads before it
                                   # writes anything (e.g. a ready/enable flag)
    }

In event mode write the trigger register in the same block as (or after) the
status register, so the status is already there when the trigger changes.

Supported SLMP commands: batch read (0401) and batch write (1401) in word and
bit units, with Q/L (0000/0001) and iQ-R (0002/0003) subcommands. Anything else
is answered with end code C059 (command not supported).
"""
from __future__ import annotations

import logging
import socketserver
import struct
import threading

from ..counters import Sample
from .base import ProtocolDriver, ProtocolError
from .slmp import (BIT_DEVICES, CMD_READ, CMD_WRITE, DEVICES, HEADER_LEN, REQ_SUBHEADER,
                   build_response, frame_length, parse_device, words_to_ascii, words_to_int)
from .tcp_listener import ANY_HOST, ListenerManager, last_peer

log = logging.getLogger("cognex.slmp_server")

END_OK = 0x0000
END_UNSUPPORTED = 0xC059
END_BAD_DEVICE = 0xC056
MAX_POINTS = 960
_BIT_CODES = {DEVICES[n][0] for n in BIT_DEVICES}


class PlcMemory:
    """Word and bit device memory of the emulated PLC. Bit devices are stored
    per bit so word and bit access to them agree."""

    def __init__(self):
        self.words: dict[int, dict[int, int]] = {}
        self.bits: dict[int, dict[int, int]] = {}

    def read_words(self, code: int, start: int, points: int) -> list[int]:
        if code in _BIT_CODES:
            out = []
            for i in range(points):
                bits = self.bits.get(code, {})
                base = start + i * 16
                out.append(sum((bits.get(base + b, 0) & 1) << b for b in range(16)))
            return out
        mem = self.words.get(code, {})
        return [mem.get(start + i, 0) for i in range(points)]

    def write_words(self, code: int, start: int, values: list[int]) -> None:
        if code in _BIT_CODES:
            bits = self.bits.setdefault(code, {})
            for i, v in enumerate(values):
                for b in range(16):
                    bits[start + i * 16 + b] = (v >> b) & 1
            return
        mem = self.words.setdefault(code, {})
        for i, v in enumerate(values):
            mem[start + i] = v & 0xFFFF

    def read_bits(self, code: int, start: int, points: int) -> list[int]:
        if code in _BIT_CODES:
            bits = self.bits.get(code, {})
            return [bits.get(start + i, 0) for i in range(points)]
        # bit access to a word device addresses its individual bits
        return [(self.read_words(code, (start + i) // 16, 1)[0] >> ((start + i) % 16)) & 1
                for i in range(points)]

    def write_bits(self, code: int, start: int, values: list[int]) -> None:
        if code in _BIT_CODES:
            bits = self.bits.setdefault(code, {})
            for i, v in enumerate(values):
                bits[start + i] = v & 1
            return
        for i, v in enumerate(values):
            addr, bit = divmod(start + i, 16)
            word = self.read_words(code, addr, 1)[0]
            word = (word | (1 << bit)) if v else (word & ~(1 << bit))
            self.write_words(code, addr, [word])

    def get(self, device: str, points: int = 1) -> list[int]:
        code, number = parse_device(device)
        return self.read_words(code, number, points)


class SlmpServerDriver(ProtocolDriver):
    key = "slmp_listen"
    label = "SLMP server (device writes to the app as if it were a PLC)"
    push = True
    push_help = ("The app acts as a Mitsubishi PLC: it opens an SLMP server (3E binary, TCP) on "
                 "this port. In the device's (e.g. In-Sight camera's) SLMP settings enter this server's IP and the port "
                 "below as the PLC, and set which registers it writes; the config below says "
                 "which register holds the job name and the counters.")
    config_fields = {
        "mode": "'counter' (device writes running totals) or 'event' (one result per part)",
        "pass_device": "Counter mode: register with the pass counter, e.g. D100",
        "fail_device": "Counter mode: register with the fail counter, e.g. D102",
        "count_device": "Counter mode: optional register with a total counter",
        "width": "1 = 16-bit values, 2 = 32-bit (two words, low word first)",
        "trigger_device": "Event mode: register that changes once per part (result ID)",
        "status_device": "Event mode: register holding the part's result",
        "pass_values": "Event mode: status values meaning pass (default [1])",
        "job_device": "Optional first register of the job name or number",
        "job_length": "Words of the ASCII job name (2 chars per word, default 8)",
        "job_format": "'ascii' (job name text) or 'number' (job id)",
        "default_job": "Job name when job_device is not set (default 'MAIN')",
        "preset": "Optional {\"D0\": 1} values the device can read before writing",
    }

    def __init__(self, host: str, port: int, config: dict | None = None):
        super().__init__(host, port, config)
        self.memory = PlcMemory()
        for device, value in (self.config.get("preset") or {}).items():
            try:
                self.memory.write_words(*parse_device(device), [int(value)])
            except (ProtocolError, TypeError, ValueError):
                log.warning("slmp_listen: ignoring bad preset %r=%r", device, value)
        self._last: tuple | None = None
        self._last_trigger: int | None = None
        self._events: dict[str, list[int]] = {}
        self._last_job: str | None = None

    def read(self) -> Sample:
        raise ProtocolError(
            "This device writes its data to the app over SLMP; it is not polled. "
            "Point the device's SLMP PLC address at this server's IP, port %s." % self.port
        )

    # ---- frames ----------------------------------------------------------
    def handle_frame(self, frame: bytes) -> tuple[bytes, bool]:
        """Answer one request frame. Returns (response, memory_was_written)."""
        route = frame[2:7]
        if frame[:2] != REQ_SUBHEADER or len(frame) < HEADER_LEN + 6:
            return build_response(route, END_UNSUPPORTED), False
        _timer, command, sub = struct.unpack_from("<HHH", frame, HEADER_LEN)
        body = frame[HEADER_LEN + 6:]
        if command not in (CMD_READ, CMD_WRITE) or sub not in (0, 1, 2, 3):
            return build_response(route, END_UNSUPPORTED), False
        bit_units = sub in (1, 3)
        try:
            if sub in (2, 3):  # iQ-R: 4-byte device number, 2-byte device code
                number = int.from_bytes(body[0:4], "little")
                code = struct.unpack_from("<H", body, 4)[0]
                points = struct.unpack_from("<H", body, 6)[0]
                data = body[8:]
            else:
                number = int.from_bytes(body[0:3], "little")
                code = body[3]
                points = struct.unpack_from("<H", body, 4)[0]
                data = body[6:]
        except (struct.error, IndexError):
            return build_response(route, END_UNSUPPORTED), False
        if code not in {c for c, _ in DEVICES.values()} or not 0 < points <= MAX_POINTS * (4 if bit_units else 1):
            return build_response(route, END_BAD_DEVICE), False

        mem = self.memory
        if command == CMD_READ:
            if bit_units:
                bits = mem.read_bits(code, number, points) + [0]
                packed = bytes((bits[i] << 4) | bits[i + 1] for i in range(0, points, 2))
                return build_response(route, END_OK, packed), False
            words = mem.read_words(code, number, points)
            return build_response(route, END_OK, struct.pack(f"<{points}H", *words)), False

        if bit_units:
            need = (points + 1) // 2
            if len(data) < need:
                return build_response(route, END_UNSUPPORTED), False
            values = []
            for byte in data[:need]:
                values += [(byte >> 4) & 1, byte & 1]
            mem.write_bits(code, number, values[:points])
        else:
            if len(data) < points * 2:
                return build_response(route, END_UNSUPPORTED), False
            mem.write_words(code, number, list(struct.unpack_from(f"<{points}H", data)))
        return build_response(route, END_OK), True

    # ---- samples ---------------------------------------------------------
    def _width(self) -> int:
        return 2 if int(self.config.get("width", 1) or 1) == 2 else 1

    def _value(self, device) -> int:
        if device in (None, ""):
            return 0
        width = self._width()
        return words_to_int(self.memory.get(str(device), width), width)

    def _job(self) -> str:
        cfg = self.config
        default = cfg.get("default_job") or "MAIN"
        device = cfg.get("job_device")
        if device in (None, ""):
            return default
        if str(cfg.get("job_format", "ascii")).lower() == "number":
            return str(self.memory.get(str(device))[0])
        length = max(1, min(int(cfg.get("job_length", 8) or 8), 64))
        return words_to_ascii(self.memory.get(str(device), length)) or default

    def sample(self) -> Sample | None:
        """The sample implied by the registers after a write, or None when
        nothing the app tracks has changed."""
        cfg = self.config
        job = self._job()
        if cfg.get("mode", "counter") == "event":
            trigger_dev = cfg.get("trigger_device")
            if not trigger_dev:
                return None
            trigger = self._value(trigger_dev)
            if trigger == self._last_trigger:
                return None
            first = self._last_trigger is None
            self._last_trigger = trigger
            if first and trigger == 0:
                return None  # nothing inspected yet, just the camera starting up
            status = self._value(cfg.get("status_device"))
            pass_values = {int(v) for v in (cfg.get("pass_values") or [1])}
            if job != self._last_job:
                self._events[job] = [0, 0]
                self._last_job = job
            tally = self._events.setdefault(job, [0, 0])
            tally[0 if status in pass_values else 1] += 1
            return Sample(job_name=job, raw_pass=tally[0], raw_fail=tally[1],
                          extra={"mode": "event", "trigger": trigger, "status": status})

        values = (job, self._value(cfg.get("pass_device")), self._value(cfg.get("fail_device")),
                  self._value(cfg.get("count_device")))
        if values == self._last:
            return None
        self._last = values
        return Sample(job_name=values[0], raw_pass=values[1], raw_fail=values[2],
                      raw_count=values[3], extra={"mode": "counter"})


# --------------------------------------------------------------------------- #
# Socket server
# --------------------------------------------------------------------------- #


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port: int, device_id: int, driver: SlmpServerDriver,
                 allowed_host: str, manager: "SlmpServerManager"):
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
            log.warning("slmp_listen port %s: rejected connection from %s (allowed %s)",
                        srv.server_address[1], peer, srv.allowed_host)
            return
        last_peer[srv.device_id] = peer
        with srv.lock:
            srv.clients += 1
        srv.manager.status(srv.device_id, True, None)
        buf = bytearray()
        try:
            while True:
                chunk = self.request.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                while True:
                    need = frame_length(buf)
                    if need is None or len(buf) < need:
                        break
                    frame = bytes(buf[:need])
                    del buf[:need]
                    self.request.sendall(self._answer(frame))
                if len(buf) > 65536:  # not SLMP; drop it
                    buf.clear()
        except OSError as exc:
            log.info("slmp_listen port %s: connection from %s ended: %s",
                     srv.server_address[1], peer, exc)
        finally:
            with srv.lock:
                srv.clients -= 1
                still = srv.clients > 0
            if not still:
                srv.manager.status(srv.device_id, False, f"Camera {peer} disconnected")

    def _answer(self, frame: bytes) -> bytes:
        srv = self.server
        with srv.manager.lock_for(srv.device_id):
            response, written = srv.driver.handle_frame(frame)
            if written:
                try:
                    sample = srv.driver.sample()
                    if sample is not None:
                        srv.manager.on_sample(srv.device_id, sample)
                except Exception as exc:  # noqa: BLE001 - keep answering the camera
                    log.warning("slmp_listen device %s: could not record sample: %s",
                                srv.device_id, exc)
        return response


class SlmpServerManager(ListenerManager):
    """One SLMP server per enabled ``slmp_listen`` camera (see ListenerManager)."""

    protocol = "slmp_listen"
    waiting = "SLMP server on TCP port {port}, waiting for the device to connect"

    def make_server(self, device_id: int, host: str, port: int, cfg: dict):
        return _Server(port, device_id, SlmpServerDriver(host, port, cfg), host.strip(), self)
