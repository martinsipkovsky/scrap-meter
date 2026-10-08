"""Developer options (Settings tab, administrators): parts of the app that use
unofficial ways into a messenger and are off unless someone turns them on.

* ``whatsapp_linked``: the WhatsApp virtual client, the app logged in as a
  linked device of a phone by QR code (app.notifiers.whatsapp_linked). Off:
  the client is stopped and the WhatsApp phone card, its groups in the Chat
  room and chat commands are hidden. The login stays in the database, so
  turning it on again reconnects the same phone.
* ``signal``: Signal through signal-cli (app.notifiers.signal). Off: the app
  doesn't talk to the Signal service; the Signal phone card and the Signal
  provider kind are hidden. The service keeps its link to the phone.

Neither deletes anything. Kept with settings_store (key dev_options). A
database from before 1.21 gets them on once (``upgrade``) when it already
uses them, so alerts keep going out after the update.
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import settings_store
from .config import settings

log = logging.getLogger("cognex.dev_options")

KEY = "dev_options"
OPTIONS = {
    "whatsapp_linked": "WhatsApp virtual client (QR login)",
    "signal": "Signal (signal-cli)",
}

_cache: dict | None = None


def load() -> dict:
    global _cache
    if _cache is None:
        stored = settings_store.load(KEY)
        _cache = {k: bool((stored or {}).get(k)) for k in OPTIONS}
    return dict(_cache)


def enabled(option: str) -> bool:
    return load().get(option, False)


def save(values: dict) -> dict:
    global _cache
    current = load()
    new = {k: bool(values.get(k, current[k])) for k in OPTIONS}
    settings_store.save(KEY, new)
    _cache = new
    return dict(new)


def _whatsapp_session() -> bool:
    """A phone was linked to the WhatsApp virtual client (its login is kept)."""
    if settings.database_url.startswith("postgresql"):
        try:
            with settings_store.engine().connect() as conn:
                return bool(conn.execute(text("SELECT count(*) FROM whatsmeow_device")).scalar())
        except Exception:  # noqa: BLE001 - no table: never linked
            return False
    return (Path(settings.data_dir) / "whatsapp.sqlite3").exists()


def upgrade(db: Session) -> dict | None:
    """Once per database: switch on what an older version already used (a
    linked WhatsApp phone, providers or the Chat room on it; Signal set up).
    Returns what was set, None when already done."""
    if settings_store.load(KEY) is not None:
        return None
    from . import chatroom
    from .models import NotificationProvider

    providers = db.query(NotificationProvider.kind, NotificationProvider.config).all()
    room = chatroom.room() or {}
    values = {
        "whatsapp_linked": _whatsapp_session()
        or any(k == "whatsapp" and (c or {}).get("transport") == "linked" for k, c in providers)
        or room.get("kind") == "whatsapp",
        "signal": bool(settings.signal_api_url) or any(k == "signal" for k, _ in providers)
        or room.get("kind") == "signal",
    }
    saved = save(values)
    if any(saved.values()):
        log.info("developer options switched on for what this server already used: %s",
                 ", ".join(OPTIONS[k] for k, v in saved.items() if v))
    return saved


def reset_cache() -> None:
    global _cache
    _cache = None
