"""The Chat room tab: one messenger chat (a WhatsApp group the linked phone is
in, a Telegram chat of a Telegram provider's bot, a Discord channel of a
Discord bot, or a Signal group of the linked Signal phone) that every user
with the chat_room permission can read and write in from the web.

* An administrator picks the room on the tab (settings key ``chat_room``:
  {"kind", "chat", "name", "provider_id"}). Changing it keeps the history; the
  tab shows the messages of the chat that is the room now.
* Messages written on the web go out through the linked WhatsApp account or
  the Telegram bot with the web user's name in front ("*Martin:* text" on
  WhatsApp, "Martin: text" on Telegram), so the chat knows who wrote. A web
  message that is a chat command ("!status") is answered in the chat like one
  typed there.
* Everything that arrives in the room's chat is kept (ChatMessage, direction
  "in"), and so is everything the app sends there: web messages, alerts and
  command replies ("out").
* WhatsApp messages come from the linked client (``on_whatsapp_message``,
  which also hands commands to app.commands). Telegram has no push to the
  app: while the room is a Telegram chat a thread asks the Bot API for new
  messages (getUpdates, long polling). In a group, a bot only gets every
  message when its privacy mode is off (@BotFather, /setprivacy) or it is an
  admin of the group; otherwise only commands and replies to it.
* Discord and Signal messages come from app.messengers (``on_message``),
  which also hands them to the commands.
"""
from __future__ import annotations

import logging
import threading

from sqlalchemy import text as sql_text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from . import settings_store
from .models import ChatMessage, Meta, NotificationProvider, User, utcnow
from .notifiers import base as notifier_base
from .notifiers import discord, signal, telegram
from .notifiers.base import NotifierError

log = logging.getLogger("cognex.chatroom")

ROOM_KEY = "chat_room"
UPGRADE_KEY = "chat_room_v1"
MAX_TEXT = 4000
KINDS = {"whatsapp": "WhatsApp", "telegram": "Telegram", "discord": "Discord", "signal": "Signal"}

_room: dict | None = None
_room_loaded = False
_lock = threading.Lock()


# ---- the room ------------------------------------------------------------------
def room() -> dict | None:
    """The chosen room, or None (cached; saving goes through set_room)."""
    global _room, _room_loaded
    if not _room_loaded:
        value = settings_store.load(ROOM_KEY)
        _room = value if isinstance(value, dict) and value.get("chat") and value.get("kind") in KINDS else None
        _room_loaded = True
    return _room


def set_room(value: dict | None) -> None:
    global _room, _room_loaded
    settings_store.save(ROOM_KEY, value)
    _room, _room_loaded = value, True
    telegram_reader.sync()


def _whatsapp_link():
    from .notifiers.whatsapp_linked import link

    return link


def _provider(db: Session, r: dict) -> NotificationProvider | None:
    pid = r.get("provider_id")
    return db.get(NotificationProvider, int(pid)) if pid is not None else None


def options(db: Session) -> dict:
    """The chats an admin can pick: WhatsApp groups of the linked phone and
    the chats of each Telegram provider. Problems are listed, not raised."""
    found, problems = [], []
    link = _whatsapp_link()
    if link.state == "connected":
        try:
            for g in link.groups():
                found.append({"kind": "whatsapp", "chat": g["id"], "name": g["name"], "provider_id": None,
                              "messenger": "WhatsApp (linked phone)"})
        except NotifierError as exc:
            problems.append(f"WhatsApp: {exc}")
    elif link.state not in ("not_linked", "unknown"):
        problems.append(f"WhatsApp is not connected ({link.error or link.state}).")
    for p in (db.query(NotificationProvider).filter(NotificationProvider.kind == "telegram")
              .order_by(NotificationProvider.id)):
        cfg = p.config or {}
        for chat in telegram.chat_list(cfg.get("chat_ids") or cfg.get("chat_id")):
            name = chat
            try:
                info = telegram.call(cfg, "getChat", {"chat_id": chat}) or {}
                name = info.get("title") or " ".join(
                    x for x in (info.get("first_name"), info.get("last_name")) if x) or chat
            except NotifierError as exc:
                problems.append(f"Telegram '{p.name}', chat {chat}: {exc}")
            found.append({"kind": "telegram", "chat": chat, "name": name, "provider_id": p.id,
                          "messenger": f"Telegram ({p.name})"})
    for p in (db.query(NotificationProvider).filter(NotificationProvider.kind == "discord")
              .order_by(NotificationProvider.id)):
        cfg = p.config or {}
        if discord.mode(cfg) != "bot":
            continue
        for chat in discord.channel_list(cfg.get("channel_ids") or cfg.get("channel_id")):
            name = chat
            try:
                name = discord.channel_name(cfg, chat)
            except NotifierError as exc:
                problems.append(f"Discord '{p.name}', channel {chat}: {exc}")
            found.append({"kind": "discord", "chat": chat, "name": name, "provider_id": p.id,
                          "messenger": f"Discord ({p.name})"})
    sig = signal.status()
    if sig["state"] == "linked":
        try:
            for g in signal.groups(refresh=True):
                found.append({"kind": "signal", "chat": g["id"], "name": g["name"], "provider_id": None,
                              "messenger": "Signal (linked phone)"})
        except NotifierError as exc:
            problems.append(f"Signal: {exc}")
    elif sig["state"] == "unreachable":
        problems.append(sig["error"])
    return {"options": found, "problems": problems}


def messenger_count(db: Session) -> int:
    """Messengers the room can use: the linked WhatsApp and Signal phones,
    Telegram providers and Discord bots."""
    n = db.query(NotificationProvider).filter(NotificationProvider.kind.in_(("telegram", "discord"))).count()
    n += 1 if signal.base_url() else 0
    return n + (1 if _whatsapp_link().state not in ("not_linked", "unknown") else 0)


def status(db: Session) -> dict:
    """The room and whether it can send and receive now."""
    r = room()
    if r is None:
        return {"room": None, "connected": False, "problem": None, "messengers": messenger_count(db)}
    problem = None
    if r["kind"] == "whatsapp":
        link = _whatsapp_link()
        if link.state != "connected":
            problem = "WhatsApp is not connected" + (f": {link.error}" if link.error else
                                                     " (link a phone on the Notifications page).")
    elif r["kind"] == "signal":
        s = signal.status()
        if s["state"] != "linked":
            problem = s["error"] or "Signal is not linked (link a phone on the Notifications page)."
        else:
            problem = _reader().errors.get("signal")
    else:
        p = _provider(db, r)
        if p is None:
            problem = f"The {KINDS[r['kind']]} provider of this room was deleted. Pick the room again."
        elif r["kind"] == "telegram" and telegram_reader.error:
            problem = telegram_reader.error
        elif r["kind"] == "discord":
            problem = _reader().errors.get(f"discord:{p.id}:{r['chat']}")
    return {"room": {**r, "messenger": KINDS[r["kind"]]}, "connected": problem is None, "problem": problem,
            "messengers": messenger_count(db)}


# ---- messages ----------------------------------------------------------------------
def messages(db: Session, after: int | None = None, before: int | None = None, limit: int = 100) -> list[dict]:
    """The room's messages, oldest first: the newest ``limit`` ones, or those
    after / before a message id."""
    r = room()
    if r is None:
        return []
    q = db.query(ChatMessage).filter(ChatMessage.kind == r["kind"], ChatMessage.chat == r["chat"])
    if after is not None:
        rows = q.filter(ChatMessage.id > after).order_by(ChatMessage.id.asc()).limit(limit).all()
    else:
        if before is not None:
            q = q.filter(ChatMessage.id < before)
        rows = list(reversed(q.order_by(ChatMessage.id.desc()).limit(limit).all()))
    return [_out(m) for m in rows]


def _out(m: ChatMessage) -> dict:
    return {"id": m.id, "direction": m.direction, "author": m.author, "user_id": m.user_id, "text": m.text,
            "status": m.status, "error": m.error, "created_at": m.created_at}


def _store(kind: str, chat: str, **fields) -> None:
    """Add a message with its own session (called from the messenger threads)."""
    from .database import SessionLocal

    db = SessionLocal()
    try:
        ext = fields.get("external_id")
        if ext and db.query(ChatMessage.id).filter(ChatMessage.kind == kind, ChatMessage.chat == chat,
                                                   ChatMessage.external_id == ext).first():
            return  # delivered again after a reconnect
        db.add(ChatMessage(kind=kind, chat=chat, **fields))
        db.commit()
    except Exception:  # noqa: BLE001 - a chat line must never break the messenger
        db.rollback()
        log.exception("could not keep a chat room message")
    finally:
        db.close()


def _is_room(kind: str, chat: str) -> bool:
    r = room()
    return r is not None and r["kind"] == kind and str(r["chat"]).strip() == str(chat or "").strip()


def on_whatsapp_message(msg: dict) -> None:
    """Every WhatsApp group message: kept when it is in the room, and handed
    to the chat commands."""
    from . import commands

    if _is_room("whatsapp", msg.get("chat", "")):
        created = None
        if msg.get("ts"):
            import datetime as dt

            created = dt.datetime.fromtimestamp(float(msg["ts"]), dt.timezone.utc)
        _store("whatsapp", msg["chat"], direction="in", author=msg.get("sender_name") or msg.get("sender"),
               text=(msg.get("text") or "")[:MAX_TEXT], status="received", external_id=msg.get("id"),
               created_at=created or utcnow())
    commands.handle_message(msg)


def on_message(kind: str, msg: dict) -> None:
    """A Discord or Signal message (app.messengers): kept when it is in the room."""
    if _is_room(kind, msg.get("chat", "")):
        import datetime as dt

        created = dt.datetime.fromtimestamp(float(msg["ts"]), dt.timezone.utc) if msg.get("ts") else utcnow()
        _store(kind, str(msg["chat"]).strip(), direction="in", author=msg.get("sender_name") or msg.get("sender"),
               text=(msg.get("text") or "")[:MAX_TEXT], status="received", external_id=str(msg.get("id") or "") or None,
               created_at=created)


def _reader():
    from .messengers import reader

    return reader


def on_sent(kind: str, chat: str, text: str) -> None:
    """The app sent a message (an alert or a command reply): kept when it went to the room."""
    if _is_room(kind, chat):
        _store(kind, str(chat).strip(), direction="out", author=None, text=text[:MAX_TEXT], status="sent")


def send(db: Session, user: User, text: str) -> dict:
    """Send a message from the web into the room, with the user's name in front."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Write a message first.")
    if len(text) > MAX_TEXT:
        raise ValueError(f"A message can have at most {MAX_TEXT} characters.")
    r = room()
    if r is None:
        raise ValueError("No chat room is set. An administrator picks one on this tab.")
    msg = ChatMessage(kind=r["kind"], chat=r["chat"], direction="out", author=user.username, user_id=user.id,
                      text=text, status="sent")
    try:
        if r["kind"] == "whatsapp":
            _whatsapp_link().send(r["chat"], f"*{user.username}:* {text}", report=False)
        elif r["kind"] == "signal":
            signal.send_to(r["chat"], f"{user.username}: {text}", report=False)
        else:
            p = _provider(db, r)
            if p is None:
                raise NotifierError(f"The {KINDS[r['kind']]} provider of this room was deleted.")
            if r["kind"] == "discord":
                discord.send_to(p.config or {}, r["chat"], f"**{user.username}:** {text}", report=False)
            else:
                telegram.send_to(p.config or {}, r["chat"], f"{user.username}: {text}", report=False)
    except NotifierError as exc:
        msg.status, msg.error = "failed", str(exc)
    db.add(msg)
    db.commit()
    db.refresh(msg)
    if msg.status == "sent" and r["kind"] != "telegram":
        _answer_command(db, r, user, text)
    return _out(msg)


def _answer_command(db: Session, r: dict, user: User, text: str) -> None:
    """A web message such as "!status" is answered in the chat like one typed there."""
    from . import commands, messengers

    if commands.parse(text, commands.get_prefix()) is None:
        return
    msg = {"chat": r["chat"], "chat_name": r.get("name"), "sender_name": user.username, "text": text,
           "kind": r["kind"]}
    if r["kind"] == "signal":
        msg["reply"] = messengers.reply_signal(r["chat"])
    elif r["kind"] == "discord":
        p = _provider(db, r)
        if p is None:
            return
        msg["reply"] = messengers.reply_discord(dict(p.config or {}), r["chat"])
    threading.Thread(target=commands.handle_message, args=(msg,), name="chatroom-command", daemon=True).start()


# ---- Telegram: new messages by long polling ------------------------------------------
class TelegramReader:
    """Asks the Bot API for new messages while the room is a Telegram chat."""

    POLL_TIMEOUT = 25  # seconds Telegram holds a getUpdates call open
    RETRY_DELAY = 15

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._offset: int | None = None
        self.error: str | None = None

    def sync(self) -> None:
        """Run while the room is a Telegram chat, stop otherwise."""
        r = room()
        if r is not None and r["kind"] == "telegram":
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="telegram-chatroom", daemon=True)
                self._thread.start()
        else:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        self.error = None

    def _config(self) -> tuple[dict | None, dict | None]:
        from .database import SessionLocal

        r = room()
        if r is None or r["kind"] != "telegram":
            return None, None
        db = SessionLocal()
        try:
            p = _provider(db, r)
            return r, (dict(p.config or {}) if p is not None else None)
        finally:
            db.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            r, cfg = self._config()
            if r is None:
                return
            if cfg is None:
                self.error = "The Telegram provider of this room was deleted. Pick the room again."
                self._stop.wait(self.RETRY_DELAY)
                continue
            try:
                body = {"timeout": self.POLL_TIMEOUT, "allowed_updates": ["message", "channel_post"]}
                if self._offset is not None:
                    body["offset"] = self._offset
                updates = telegram.call(cfg, "getUpdates", body, timeout=self.POLL_TIMEOUT + 10) or []
                self.error = None
            except NotifierError as exc:
                self.error = f"Telegram: {exc}"
                log.warning("chat room: %s", self.error)
                self._stop.wait(self.RETRY_DELAY)
                continue
            for u in updates:
                self._offset = max(self._offset or 0, int(u.get("update_id", 0)) + 1)
                self.handle(r, u)

    @staticmethod
    def handle(r: dict, update: dict) -> None:
        m = update.get("message") or update.get("channel_post")
        if not m or str(m.get("chat", {}).get("id")) != str(r["chat"]).strip():
            return
        text = m.get("text") or m.get("caption") or ""
        if not text:
            for field, label in (("photo", "photo"), ("video", "video"), ("document", "file"),
                                 ("voice", "voice message"), ("sticker", "sticker")):
                if m.get(field):
                    text = f"[{label}]"
                    break
        if not text:
            return
        who = m.get("from") or {}
        author = (" ".join(x for x in (who.get("first_name"), who.get("last_name")) if x)
                  or who.get("username") or m.get("chat", {}).get("title"))
        import datetime as dt

        created = dt.datetime.fromtimestamp(m["date"], dt.timezone.utc) if m.get("date") else utcnow()
        _store("telegram", str(r["chat"]).strip(), direction="in", author=author, text=text[:MAX_TEXT],
               status="received", external_id=str(m.get("message_id")), created_at=created)


telegram_reader = TelegramReader()


# ---- setup ------------------------------------------------------------------------------
def install() -> None:
    """Hook the room into the messengers (app start)."""
    link = _whatsapp_link()
    link.on_message = on_whatsapp_message
    link.on_sent = lambda to, text: on_sent("whatsapp", to, text)
    notifier_base.on_sent = on_sent
    telegram_reader.sync()


def upgrade(engine: Engine) -> int:
    """Every user can use the Chat room by default: give the new permission to
    the users of a database from before it existed (once per database).
    Returns the number of users given it."""
    with engine.connect() as conn:
        if conn.execute(sql_text("SELECT 1 FROM meta WHERE key = :k"), {"k": UPGRADE_KEY}).first():
            return 0
    n = 0
    with Session(bind=engine) as db:
        for user in db.query(User).filter(User.is_admin.isnot(True)).all():
            perms = list(user.permissions or [])
            if "chat_room" not in perms:
                user.permissions = perms + ["chat_room"]
                n += 1
        db.merge(Meta(key=UPGRADE_KEY, value=utcnow().isoformat()))
        db.commit()
    if n:
        log.info("upgrade: %d user(s) can use the new Chat room", n)
    return n

