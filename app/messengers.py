"""Messages arriving in Discord channels and Signal groups.

Neither pushes to the app, so a thread asks them for new messages every few
seconds (POLL_S):

* Discord: the channels of every enabled Discord provider in bot mode
  (app.notifiers.discord.new_messages). It starts at the newest message of a
  channel, so nothing written before the app started is answered.
* Signal: the linked phone's group messages (app.notifiers.signal.receive),
  when the Signal service is set up.

Each message goes to the Chat room (kept when its chat is the room) and to
the chat commands, which answer in the same channel or group. Messages of the
app's own Discord bot are left out.
"""
from __future__ import annotations

import logging
import threading
import time

from .notifiers import discord, signal
from .notifiers.base import NotifierError

log = logging.getLogger("cognex.messengers")

POLL_S = 3.0


def reply_discord(config: dict, channel: str):
    return lambda text: discord.send_to(config, channel, text)


def reply_signal(chat: str):
    return lambda text: signal.send_to(chat, text)


class Reader:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._after: dict[tuple[int, str], str | None] = {}  # (provider id, channel) -> last message id
        self._bot_ids: dict[int, str] = {}
        # problems by "discord:<provider id>:<channel>" / "signal", shown in the Chat room
        self.errors: dict[str, str] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="messengers", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - never let the loop die
                log.exception("reading the messengers failed")
            self._stop.wait(POLL_S)

    # ---- one round ----
    def poll_once(self) -> int:
        """Read every channel and group once; returns the messages handled."""
        return self._poll_discord() + self._poll_signal()

    def _discord_providers(self) -> list[tuple[int, str, dict]]:
        from .database import SessionLocal
        from .models import NotificationProvider

        db = SessionLocal()
        try:
            rows = (db.query(NotificationProvider)
                    .filter(NotificationProvider.kind == "discord", NotificationProvider.enabled.is_(True)).all())
            return [(p.id, p.name, dict(p.config or {})) for p in rows if discord.mode(p.config or {}) == "bot"]
        finally:
            db.close()

    def _poll_discord(self) -> int:
        handled = 0
        seen = set()
        for pid, pname, cfg in self._discord_providers():
            for channel in discord.channel_list(cfg.get("channel_ids") or cfg.get("channel_id")):
                key = (pid, channel)
                seen.add(key)
                err_key = f"discord:{pid}:{channel}"
                try:
                    if pid not in self._bot_ids:
                        self._bot_ids[pid] = str(discord.bot_user(cfg).get("id") or "")
                    rows = discord.new_messages(cfg, channel, self._after.get(key))
                    self.errors.pop(err_key, None)
                except NotifierError as exc:
                    self.errors[err_key] = f"Discord '{pname}', channel {channel}: {exc}"
                    continue
                if key not in self._after:  # first look: start after the newest message
                    self._after[key] = rows[-1]["id"] if rows else "0"
                    continue
                for m in rows:
                    self._after[key] = m["id"]
                    author = m.get("author") or {}
                    if author.get("bot") or str(author.get("id")) == self._bot_ids.get(pid):
                        continue
                    text = m.get("content") or ("[attachment]" if m.get("attachments") else "")
                    if not text:
                        continue
                    ts = _discord_ts(m.get("timestamp"))
                    deliver("discord", {
                        "chat": channel, "chat_name": channel, "id": m["id"], "ts": ts, "text": text,
                        "sender": author.get("id"), "sender_name": author.get("global_name") or author.get("username"),
                        "reply": reply_discord(cfg, channel), "provider_id": pid,
                    })
                    handled += 1
        for key in list(self._after):
            if key not in seen:  # a channel or provider was removed
                del self._after[key]
        return handled

    def _poll_signal(self) -> int:
        if not signal.base_url():
            return 0
        try:
            rows = signal.receive()
            self.errors.pop("signal", None)
        except NotifierError as exc:
            self.errors["signal"] = f"Signal: {exc}"
            return 0
        for m in rows:
            deliver("signal", {**m, "reply": reply_signal(m["chat"])})
        return len(rows)


def _discord_ts(value: str | None) -> float:
    import datetime as dt

    try:
        return dt.datetime.fromisoformat(value).timestamp() if value else time.time()
    except ValueError:
        return time.time()


def deliver(kind: str, msg: dict) -> None:
    """One incoming message: the Chat room keeps it, the commands answer it."""
    from . import chatroom, commands

    chatroom.on_message(kind, msg)
    commands.handle_message({**msg, "kind": kind})


reader = Reader()
