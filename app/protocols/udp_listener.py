"""UDP listener (camera pushes datagrams to the app) driver.

The UDP twin of the TCP listener: the app opens a UDP socket on the camera's
configured port and the camera (In-Sight "UDP" Data Channel / TCP/IP device
set to UDP, or anything that can send a datagram) sends one ASCII record per
inspection to this server's IP and that port. There is no connection, so
nothing has to be "connected" first and a datagram lost on the network is
simply missed (in ``counter`` mode the next record carries the totals again,
so nothing is lost for good; in ``event`` mode that part is not counted).

On the Cameras tab:

* **Port** is the UDP port the app listens on, inside ``LISTEN_PORTS``
  (default 5100-5119; docker-compose publishes the range for TCP *and* UDP).
  Each listening camera needs its own port.
* **Host** is optional: the camera's IP to accept data from. Leave it blank
  (or ``0.0.0.0``) to accept any sender.

The record layout and both modes are exactly the TCP listener's (same parser,
see ``tcp_listener.py``), plus one UDP-only option::

    config = {
        "mode": "counter",          # or "event"
        "delimiter": ",", "terminator": "\\r\\n",
        "job_field": 0, "pass_field": 1, "fail_field": 2,
        "count_field": null, "default_job": "MAIN",
        "status_field": 1, "pass_values": ["pass", "ok", "1", "good", "p"],
        "offline_after": 0          # seconds of silence before the camera is
                                    # shown offline; 0 = never (a stopped line
                                    # is not a fault)
    }

A datagram may hold one record, or several separated by the terminator; a
datagram without any terminator is taken as one whole record.
"""
from __future__ import annotations

import logging
import socketserver
import time

from .tcp_listener import ANY_HOST, ListenerManager, TcpListenerDriver, last_peer

log = logging.getLogger("cognex.udp_listener")


class UdpListenerDriver(TcpListenerDriver):
    key = "udp_listen"
    label = "UDP listener (device pushes datagrams)"
    push = True
    push_help = ("The app opens a UDP port and the device sends its result to it "
                 "(In-Sight: UDP output pointing at this server's IP and the port below). "
                 "Each datagram is one record split by the delimiter; set which field holds "
                 "the job name and the counters below.")
    config_fields = {
        **TcpListenerDriver.config_fields,
        "terminator": "Record terminator inside a datagram (default CRLF); optional, "
                      "a datagram without one is one record",
        "offline_after": "Seconds without a datagram before the device shows offline (0 = never)",
    }

    def records(self, data: bytes) -> list[str]:
        """Every record in one datagram."""
        buf = bytearray(data)
        out = self.split_records(buf)
        tail = bytes(buf).decode("ascii", errors="replace").strip()
        if tail:
            out.append(tail)
        return out


class _UdpServer(socketserver.UDPServer):
    """One socket per camera. Datagrams are handled one at a time, in order,
    which keeps event-mode tallies exact."""

    allow_reuse_address = True

    def __init__(self, port: int, device_id: int, driver: UdpListenerDriver,
                 allowed_host: str, manager: "UdpListenerManager"):
        self.device_id = device_id
        self.driver = driver
        self.allowed_host = allowed_host
        self.manager = manager
        self.online = False
        self.last_rx = 0.0
        try:
            self.offline_after = float(driver.config.get("offline_after") or 0)
        except (TypeError, ValueError):
            self.offline_after = 0.0
        super().__init__(("0.0.0.0", port), _Handler)

    def service_actions(self) -> None:
        # called by serve_forever between datagrams (every poll_interval)
        if (self.online and self.offline_after > 0
                and time.monotonic() - self.last_rx > self.offline_after):
            self.online = False
            self.manager.status(
                self.device_id, False,
                f"No data for {self.offline_after:g} s on UDP port {self.server_address[1]}")


class _Handler(socketserver.BaseRequestHandler):
    server: _UdpServer

    def handle(self) -> None:
        srv = self.server
        data, _sock = self.request
        peer = self.client_address[0]
        if srv.allowed_host not in ANY_HOST and peer != srv.allowed_host:
            log.warning("udp_listen port %s: ignored datagram from %s (allowed %s)",
                        srv.server_address[1], peer, srv.allowed_host)
            return
        srv.last_rx = time.monotonic()
        last_peer[srv.device_id] = peer
        if not srv.online:
            srv.online = True
            srv.manager.status(srv.device_id, True, None)
        for line in srv.driver.records(data):
            try:
                with srv.manager.lock_for(srv.device_id):
                    sample = srv.driver.parse(line)
                    srv.manager.on_sample(srv.device_id, sample)
            except Exception as exc:  # noqa: BLE001 - one bad record must not stop the port
                log.warning("udp_listen device %s: could not process %r: %s",
                            srv.device_id, line, exc)


class UdpListenerManager(ListenerManager):
    """One UDP socket per enabled ``udp_listen`` camera (see ListenerManager)."""

    protocol = "udp_listen"
    waiting = "Listening on UDP port {port}, waiting for the first datagram"

    def make_server(self, device_id: int, host: str, port: int, cfg: dict):
        return _UdpServer(port, device_id, UdpListenerDriver(host, port, cfg), host.strip(), self)
