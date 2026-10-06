"""WhatsApp linked device: the app logs in to WhatsApp as one of your phone's
linked devices (like WhatsApp Web) and sends alerts from your own number,
into groups too.

UNOFFICIAL: this uses the reverse-engineered WhatsApp Web protocol through
the neonize library (Python bindings for whatsmeow, written in Go). WhatsApp
does not allow unofficial clients and may restrict or ban numbers that use
them. A few alert messages a day carry little risk, but use a number you can
afford to lose rather than your main one.

How it runs
-----------
The client runs in a child process (``_child``), not in the web server
process: the Go library keeps its own threads that cannot always be stopped
(a QR login in progress ignores Stop), and killing a process always works.
The parent (``WhatsAppLink``, one per app, ``link``) starts the child, keeps
the state the Notifications page shows (QR code, linked number) and sends it
commands over a multiprocessing pipe:

    parent -> child   {"id": 1, "cmd": "send", "to": "...", "text": "..."}
                      {"id": 2, "cmd": "groups"}  /  {"id": 3, "cmd": "logout"}
    child -> parent   {"id": 1, "ok": true, "result": ...} or {"id": 1, "ok": false, "error": "..."}
                      {"event": "qr", "code": "..."}  /  {"event": "connected", "phone": ..., "name": ...}
                      {"event": "logged_out", "reason": "..."}  /  {"event": "not_linked"}  / ...
                      {"event": "message", "chat": "…@g.us", "chat_name": ..., "sender": ...,
                       "sender_name": ..., "from_me": ..., "text": "!status"}

Incoming messages: only group text messages that start with a punctuation
character (a possible command prefix such as "!") and are less than two
minutes old are passed to the parent, which hands them to ``on_message``
(app.commands). Messages the app sent itself are never passed on, so a reply
can not trigger another command.

The login (the session keys) is stored by whatsmeow in the environment
database (its own whatsmeow_* tables in the Postgres container, which is
always kept in a docker volume), so it survives restarts and image updates.
Without Postgres (development) it is a SQLite file in DATA_DIR.

Troubleshooting: the child's log lines go to the container log
(``docker compose logs web``), prefixed with "whatsapp".
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import queue
import threading
import time
from pathlib import Path

from sqlalchemy.engine import make_url

from ..config import settings
from .base import NotifierError

log = logging.getLogger("cognex.whatsapp")

CLIENT_ID = "cognex-monitor"
DEVICE_NAME = "Scrap Meter"  # shown in the phone's list of linked devices
PAIR_TIMEOUT = 170  # seconds WhatsApp keeps offering QR codes for one login
REQUEST_TIMEOUT = 30
MESSAGE_MAX_AGE = 120  # seconds; older messages (delivered after a reconnect) are ignored
RESTART_DELAY = 20  # seconds before a linked client that stopped is started again,
RESTART_DELAY_MAX = 600  # doubling after each failure up to this


def session_store() -> str:
    """Where whatsmeow keeps the login: a postgres:// URL or a SQLite path."""
    url = settings.database_url
    if url.startswith("postgresql"):
        u = make_url(url)
        query = dict(u.query)
        query.setdefault("sslmode", "prefer")
        return u.set(drivername="postgres", query=query).render_as_string(hide_password=False)
    path = Path(settings.data_dir) / "whatsapp.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path.resolve())


# --------------------------------------------------------------------------- #
# Child process
# --------------------------------------------------------------------------- #


def _jid(to: str):
    """A group id (…@g.us) or a phone number with country code."""
    from neonize.utils.jid import build_jid

    to = (to or "").strip()
    if "@" in to:
        user, server = to.split("@", 1)
        return build_jid(user, server)
    digits = "".join(ch for ch in to if ch.isdigit())
    if not digits:
        raise ValueError(f"'{to}' is neither a group id (…@g.us) nor a phone number")
    return build_jid(digits)


def _child(conn, store: str, pair: bool) -> None:  # pragma: no cover - needs WhatsApp
    import os

    logging.basicConfig(level=logging.INFO, format="whatsapp %(levelname)s %(name)s: %(message)s")
    # alerts are text only, so the missing ffmpeg (media messages) does not matter
    logging.getLogger("neonize.utils.ffmpeg").setLevel(logging.ERROR)
    lock = threading.Lock()

    def emit(msg: dict) -> None:
        with lock:
            conn.send(msg)

    try:
        from neonize.client import ClientFactory, NewClient
        from neonize.events import (
            ConnectedEv,
            ConnectFailureEv,
            LoggedOutEv,
            MessageEv,
            PairStatusEv,
            StreamReplacedEv,
            TemporaryBanEv,
        )
        from neonize.proto.waCompanionReg.WAWebProtobufsCompanionReg_pb2 import DeviceProps
        from neonize.utils.jid import Jid2String, JIDToNonAD
    except Exception as exc:  # noqa: BLE001
        emit({"event": "error", "error": f"WhatsApp library is not available: {exc}"})
        return

    try:
        linked = bool(ClientFactory.get_all_devices_from_db(store))
    except Exception as exc:  # noqa: BLE001
        emit({"event": "error", "error": f"cannot open the WhatsApp login store: {exc}"})
        return
    if not linked and not pair:
        emit({"event": "not_linked"})
        return

    client = NewClient(store, uuid=CLIENT_ID,
                       props=DeviceProps(os=DEVICE_NAME, platformType=DeviceProps.DESKTOP))

    def me() -> dict:
        try:
            dev = client.get_me()
            return {"phone": dev.JID.User or None, "name": dev.PushName or None}
        except Exception:  # noqa: BLE001
            return {}

    client.event.qr(lambda _c, code: emit({"event": "qr", "code": code.decode()}))

    @client.event(ConnectedEv)
    def _connected(_c, _ev):
        emit({"event": "connected", **me()})

    @client.event(PairStatusEv)
    def _paired(_c, ev):
        if ev.Error:
            emit({"event": "error", "error": f"linking failed: {ev.Error}"})
        else:
            emit({"event": "paired", "phone": ev.ID.User or None})

    @client.event(LoggedOutEv)
    def _logged_out(_c, ev):
        emit({"event": "logged_out", "reason": str(ev.Reason)})
        os._exit(0)

    @client.event(StreamReplacedEv)
    def _replaced(_c, _ev):
        emit({"event": "error", "error": "another program logged in with this WhatsApp link"})
        os._exit(0)

    @client.event(TemporaryBanEv)
    def _banned(_c, ev):
        emit({"event": "error", "error": f"WhatsApp temporarily banned this number (code {ev.Code}, {ev.Expire} s)"})

    @client.event(ConnectFailureEv)
    def _failed(_c, ev):
        emit({"event": "error", "error": f"connection refused by WhatsApp: {ev.Message or ev.Reason}"})

    sent_ids: list[str] = []  # ids of messages the app sent (never commands)
    group_names: dict[str, str] = {}

    def group_name(jid) -> str | None:
        key = Jid2String(jid)
        if key not in group_names:
            try:
                group_names[key] = client.get_group_info(jid).GroupName.Name or key
            except Exception:  # noqa: BLE001
                return None
        return group_names[key]

    @client.event(MessageEv)
    def _message(_c, ev):
        try:
            src = ev.Info.MessageSource
            if not src.IsGroup or ev.Info.ID in sent_ids:
                return
            m = ev.Message
            text = (m.conversation or m.extendedTextMessage.text or "").strip()
            if not text or text[0].isalnum() or len(text) > 500:
                return
            ts = ev.Info.Timestamp
            ts = ts / 1000 if ts > 10**11 else ts  # seconds or milliseconds
            if ts and time.time() - ts > MESSAGE_MAX_AGE:
                return
            chat = JIDToNonAD(src.Chat)
            info = {"event": "message", "chat": Jid2String(chat), "sender": src.Sender.User or None,
                    "sender_name": ev.Info.Pushname or None, "from_me": bool(src.IsFromMe), "text": text}
            # looking up the group name is a request to WhatsApp: not on the event thread
            threading.Thread(target=lambda: emit({**info, "chat_name": group_name(chat)}), daemon=True).start()
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("whatsapp").warning("could not read a message: %s", exc)

    def run() -> None:
        try:
            client.connect()
        except Exception as exc:  # noqa: BLE001
            emit({"event": "error", "error": str(exc)})
        os._exit(0)

    threading.Thread(target=run, name="whatsapp-connect", daemon=True).start()
    emit({"event": "starting", "pairing": not linked})

    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            os._exit(0)  # the web app is gone
        rid = msg.get("id")
        try:
            cmd = msg.get("cmd")
            if cmd == "send":
                resp = client.send_message(_jid(msg["to"]), msg["text"])
                sent_ids.append(resp.ID)
                del sent_ids[:-200]
                result = True
            elif cmd == "groups":
                result = sorted(
                    ({"id": Jid2String(g.JID), "name": g.GroupName.Name or Jid2String(g.JID)}
                     for g in client.get_joined_groups()),
                    key=lambda g: g["name"].lower(),
                )
            elif cmd == "logout":
                client.logout()
                result = True
            else:
                raise ValueError(f"unknown command {cmd}")
            emit({"id": rid, "ok": True, "result": result})
        except Exception as exc:  # noqa: BLE001
            emit({"id": rid, "ok": False, "error": str(exc) or exc.__class__.__name__})


# --------------------------------------------------------------------------- #
# Parent (web app)
# --------------------------------------------------------------------------- #


class WhatsAppLink:
    """One linked WhatsApp account for the whole app."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._proc = None
        self._conn = None
        self._next_id = 0
        self._pending: dict[int, dict] = {}
        self._stopping = False
        self._restart_timer: threading.Timer | None = None
        self._failures = 0
        self.state = "unknown"  # unknown | not_linked | starting | qr | connected | error
        self.qr: str | None = None
        self.qr_at: float | None = None
        self.phone: str | None = None
        self.name: str | None = None
        self.error: str | None = None
        self.pairing = False
        self.since: float | None = None
        # called with each incoming group message that may be a command
        # (set by app.main to app.commands.handle_message); runs on its own
        # thread, one message at a time
        self.on_message = None
        self._inbox: "queue.Queue[dict]" = queue.Queue()
        self._worker: threading.Thread | None = None

    # ---- process management ------------------------------------------------
    def running(self) -> bool:
        return self._proc is not None and self._proc.is_alive()

    def start(self, pair: bool = False) -> None:
        """Start the client. pair=True shows a QR code if no phone is linked yet."""
        with self._lock:
            if self._restart_timer:
                self._restart_timer.cancel()
                self._restart_timer = None
            if self.running():
                if not pair or self.state in ("qr", "connected", "starting"):
                    return
                self._kill()
            ctx = mp.get_context("spawn")
            parent, child = ctx.Pipe()
            proc = ctx.Process(target=_child, args=(child, session_store(), pair),
                               name="whatsapp", daemon=True)
            proc.start()
            child.close()
            self._proc, self._conn, self._stopping = proc, parent, False
            self.state, self.error, self.qr, self.pairing = "starting", None, None, pair
            self.since = time.time()
            threading.Thread(target=self._reader, args=(proc, parent), name="whatsapp-reader",
                             daemon=True).start()
            if pair:
                threading.Timer(PAIR_TIMEOUT, self._pair_timeout, args=(proc,)).start()

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
            if self._restart_timer:
                self._restart_timer.cancel()
                self._restart_timer = None
            self._kill()

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.is_alive():
            proc.kill()
            proc.join(5)
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None
        self._fail_pending("the WhatsApp client was stopped")

    def _pair_timeout(self, proc) -> None:
        with self._lock:
            if self._proc is proc and self.state in ("qr", "starting"):
                log.info("QR code was not scanned in time")
                self._stopping = True
                self._kill()
                self.state, self.qr = "not_linked", None
                self.error = "The QR code expired before it was scanned. Press Link phone to get a new one."

    def _reader(self, proc, conn) -> None:
        while True:
            try:
                msg = conn.recv()
            except (EOFError, OSError):
                break
            self._handle(msg)
        proc.join(5)
        with self._lock:
            if self._proc is not proc and self._proc is not None:
                return  # replaced by a newer process
            self._proc, self._conn = None, None
            self._fail_pending("the WhatsApp client stopped")
            if self._stopping or self.state in ("not_linked", "unknown"):
                return
            if self.pairing:
                # a login that did not finish: back to "not linked"
                self.state, self.qr = ("error" if self.error else "not_linked"), None
                return
            # a linked client stopped (connection refused, WhatsApp restart):
            # try again, waiting longer after each failure
            delay = min(RESTART_DELAY * 2 ** self._failures, RESTART_DELAY_MAX)
            self._failures += 1
            self.state = "error"
            self.error = (self.error or "The WhatsApp client stopped.") + f" Reconnecting in {delay} s."
            log.warning("WhatsApp client stopped, restarting in %d s", delay)
            self._restart_timer = threading.Timer(delay, self.start)
            self._restart_timer.daemon = True
            self._restart_timer.start()

    def _handle(self, msg: dict) -> None:
        with self._lock:
            if "id" in msg:
                waiter = self._pending.pop(msg["id"], None)
                if waiter is not None:
                    waiter["reply"] = msg
                    waiter["event"].set()
                return
            ev = msg.get("event")
            if ev == "message":
                self._dispatch(msg)
            elif ev == "qr":
                self.state, self.qr, self.qr_at = "qr", msg["code"], time.time()
            elif ev == "paired":
                self.state, self.qr, self.phone = "starting", None, msg.get("phone")
            elif ev == "connected":
                self.state, self.qr, self.error, self.pairing = "connected", None, None, False
                self._failures = 0
                self.phone = msg.get("phone") or self.phone
                self.name = msg.get("name") or self.name
                self.since = time.time()
                log.info("WhatsApp linked as %s", self.phone)
            elif ev == "not_linked":
                self.state, self.phone, self.name = "not_linked", None, None
            elif ev == "logged_out":
                self.state, self.phone, self.name, self.qr = "not_linked", None, None, None
                self.error = "The phone logged this app out (removed it from Linked devices)."
            elif ev == "error":
                self.error = msg.get("error")
                log.warning("WhatsApp: %s", self.error)
                if self.state != "connected":
                    self.state = "error"

    def _dispatch(self, msg: dict) -> None:
        if self.on_message is None:
            return
        self._inbox.put(msg)
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._work, name="whatsapp-commands", daemon=True)
            self._worker.start()

    def _work(self) -> None:
        while True:
            msg = self._inbox.get()
            try:
                self.on_message(msg)
            except Exception:  # noqa: BLE001 - one bad command must not stop the others
                log.exception("handling a WhatsApp message failed")

    def _fail_pending(self, reason: str) -> None:
        for waiter in self._pending.values():
            waiter["reply"] = {"ok": False, "error": reason}
            waiter["event"].set()
        self._pending.clear()

    # ---- commands ----------------------------------------------------------
    def request(self, cmd: str, timeout: float = REQUEST_TIMEOUT, **args):
        with self._lock:
            if not self.running() or self._conn is None:
                raise NotifierError("WhatsApp is not linked. Link a phone on the Notifications page.")
            if self.state != "connected":
                raise NotifierError(f"WhatsApp is not connected ({self.error or self.state}).")
            self._next_id += 1
            rid = self._next_id
            waiter = {"event": threading.Event(), "reply": None}
            self._pending[rid] = waiter
            self._conn.send({"id": rid, "cmd": cmd, **args})
        if not waiter["event"].wait(timeout):
            with self._lock:
                self._pending.pop(rid, None)
            raise NotifierError(f"WhatsApp did not answer within {int(timeout)} s")
        reply = waiter["reply"]
        if not reply.get("ok"):
            raise NotifierError(f"WhatsApp: {reply.get('error')}")
        return reply.get("result")

    def send(self, to: str, text: str) -> None:
        if not (to or "").strip():
            raise NotifierError("set 'to' to a group id (…@g.us) or a phone number")
        self.request("send", to=to, text=text)

    def groups(self) -> list[dict]:
        return self.request("groups")

    def logout(self) -> None:
        """Unlink (needs a connection, WhatsApp is told so the phone forgets it)."""
        self.request("logout")
        self.stop()
        self.state, self.phone, self.name, self.qr, self.error = "not_linked", None, None, None, None

    def status(self) -> dict:
        with self._lock:
            qr_svg = None
            if self.state == "qr" and self.qr:
                try:
                    import segno

                    qr_svg = segno.make_qr(self.qr).svg_data_uri(scale=6, border=2)
                except Exception as exc:  # noqa: BLE001
                    self.error = f"cannot draw the QR code: {exc}"
            return {
                "state": self.state,
                "qr": qr_svg,
                "phone": self.phone,
                "name": self.name,
                "error": self.error,
                "running": self.running(),
                "since": self.since,
            }


link = WhatsAppLink()
