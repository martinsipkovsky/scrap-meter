"""Power BI access switched on from the Database tab, without a compose file.

When an administrator turns it on, the app
* makes the read-only login (app.reporting.ensure_reader) with a generated
  password, kept in the settings (settings_store key ACCESS_KEY), and
* opens a TCP port that passes every connection on to the database
  (``Forwarder``). The port is one of the listener ports (LISTEN_PORTS),
  which the compose file already publishes, so nothing on the server has to
  change. Power BI connects to <server>:<port> as if it were the database.
  Only the read-only login gets through: the port reads the connection's
  first message (PostgreSQL's startup message, which names the user) and
  closes any other user's connection, so the app's own database login can't
  be used from outside. Encryption requests are answered "no" by the port
  itself (the bundled database has no TLS anyway), so that message can be
  read.

Both are set up again on every start while it is on. Turning it off closes
the port and takes the login's right to sign in away (NOLOGIN).

The older setup (POWERBI_PASSWORD in the .env with the compose override that
publishes the database) still works and is left alone: its password is used
for the login, and turning the switch off doesn't lock it.
"""
from __future__ import annotations

import logging
import secrets
import select
import socket
import socketserver
import struct
import threading

from sqlalchemy import text
from sqlalchemy.engine import Engine

from . import dbconfig, reporting, settings_store
from .config import settings
from .protocols.tcp_listener import parse_port_range

log = logging.getLogger("cognex.powerbi")

ACCESS_KEY = "powerbi_access"  # {"enabled": bool, "port": int, "password": str}


def load() -> dict:
    value = settings_store.load(ACCESS_KEY)
    return value if isinstance(value, dict) else {}


def default_port() -> int:
    return parse_port_range(settings.listen_ports)[1]  # the last listener port


def env_configured() -> bool:
    return bool(settings.powerbi_password)


def password() -> str | None:
    if env_configured():
        return settings.powerbi_password
    return load().get("password") or None


def reserved_port() -> int | None:
    """The listener port Power BI uses while it is on (devices can't take it)."""
    cfg = load()
    return int(cfg.get("port") or default_port()) if cfg.get("enabled") else None


# ---- the port -----------------------------------------------------------------
_SSL_REQUEST, _GSS_REQUEST, _CANCEL_REQUEST = 80877103, 80877104, 80877102
_PROTOCOL_3 = 196608


def _read_exact(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("closed")
        data += chunk
    return data


def _first_message(sock: socket.socket) -> bytes | None:
    """The startup (or cancel) message, after answering encryption requests
    with "N". None when it is neither."""
    for _ in range(3):
        head = _read_exact(sock, 8)
        length, code = struct.unpack("!ii", head)
        if not 8 <= length <= 10000:
            return None
        body = _read_exact(sock, length - 8)
        if code in (_SSL_REQUEST, _GSS_REQUEST):
            sock.sendall(b"N")
            continue
        if code in (_PROTOCOL_3, _CANCEL_REQUEST):
            return head + body
        return None
    return None


def startup_user(message: bytes) -> str | None:
    """The user named in a startup message; None for a cancel request."""
    if struct.unpack("!i", message[4:8])[0] != _PROTOCOL_3:
        return None
    parts = message[8:].split(b"\0")
    for key, value in zip(parts[0::2], parts[1::2]):
        if key == b"user":
            return value.decode("utf-8", "replace")
    return ""


def _refuse(sock: socket.socket, text_: str) -> None:
    """An ErrorResponse, so Power BI shows why."""
    fields = b"SFATAL\0C28000\0M" + text_.encode() + b"\0\0"
    try:
        sock.sendall(b"E" + struct.pack("!i", len(fields) + 4) + fields)
    except OSError:
        pass


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        target = self.server.target  # type: ignore[attr-defined]
        allowed = self.server.user  # type: ignore[attr-defined]
        self.request.settimeout(30)
        try:
            first = _first_message(self.request)
        except (OSError, ConnectionError, struct.error):
            return
        if first is None:
            return
        user = startup_user(first)
        if user is not None and user != allowed:
            log.warning("Power BI port: refused user '%s' from %s", user, self.client_address[0])
            _refuse(self.request, f"Only the read-only login '{allowed}' can connect on this port")
            return
        try:
            upstream = socket.create_connection(target, timeout=10)
            upstream.sendall(first)
        except OSError as exc:
            log.warning("Power BI connection from %s: database %s:%s not reachable: %s",
                        self.client_address[0], *target, exc)
            return
        upstream.settimeout(None)
        self.request.settimeout(None)
        pair = {self.request: upstream, upstream: self.request}
        try:
            while True:
                ready, _, _ = select.select(list(pair), [], [], 60)
                if self.server.closing.is_set():  # type: ignore[attr-defined]
                    return
                for sock in ready:
                    data = sock.recv(65536)
                    if not data:
                        return
                    pair[sock].sendall(data)
        except OSError:
            return
        finally:
            upstream.close()


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class Forwarder:
    def __init__(self) -> None:
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self.port: int | None = None
        self.error: str | None = None

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self, port: int, target: tuple[str, int], user: str = "powerbi") -> None:
        self.stop()
        try:
            server = _Server(("0.0.0.0", port), _Handler)
        except OSError as exc:
            self.error = f"Port {port} could not be opened: {exc}"
            log.warning(self.error)
            return
        server.target = target  # type: ignore[attr-defined]
        server.user = user  # type: ignore[attr-defined]
        server.closing = threading.Event()  # type: ignore[attr-defined]
        self._server, self.port, self.error = server, port, None
        self._thread = threading.Thread(target=server.serve_forever, name="cognex-powerbi", daemon=True)
        self._thread.start()
        log.info("Power BI port %s passes connections on to the database at %s:%s", port, *target)

    def stop(self) -> None:
        server, self._server, self.port = self._server, None, None
        if server is not None:
            server.closing.set()  # type: ignore[attr-defined]
            server.shutdown()
            server.server_close()


forwarder = Forwarder()


# ---- switching it on and off ----------------------------------------------------
def _target(url: str) -> tuple[str, int]:
    d = dbconfig.describe_url(url)
    return d["host"] or "localhost", int(d["port"] or 5432)


def _lock_login(engine: Engine) -> None:
    user = settings.powerbi_user
    with engine.begin() as conn:
        if conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :u"), {"u": user}).first():
            conn.execute(text(f"ALTER ROLE {user} NOLOGIN"))


def apply(engine: Engine, url: str) -> str | None:
    """Open or close the port and set up or lock the login, as saved.
    Returns an error to show, or None."""
    cfg = load()
    if engine.dialect.name != "postgresql":
        forwarder.stop()
        return "Reports need PostgreSQL; the app uses a local SQLite file."
    if not cfg.get("enabled"):
        forwarder.stop()
        if not env_configured():
            try:
                _lock_login(engine)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not lock the read-only login: %s", exc)
        return None
    try:
        reporting.ensure_reader(engine, settings.powerbi_user, password())
    except Exception as exc:  # noqa: BLE001 - e.g. the database user may not create roles
        forwarder.stop()
        return f"The read-only login could not be set up: {exc}"
    forwarder.start(int(cfg.get("port") or default_port()), _target(url), settings.powerbi_user)
    return forwarder.error


def save(enabled: bool, port: int | None = None, new_password: bool = False) -> dict:
    cfg = load()
    cfg["enabled"] = enabled
    cfg["port"] = int(port or cfg.get("port") or default_port())
    if new_password or not cfg.get("password"):
        cfg["password"] = secrets.token_urlsafe(18)
    settings_store.save(ACCESS_KEY, cfg)
    return cfg
