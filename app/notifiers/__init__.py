"""Notifier registry (see app.protocols for the same pattern)."""
from __future__ import annotations

from .base import Notifier, NotifierError
from .discord import DiscordNotifier
from .signal import SignalNotifier
from .telegram import TelegramNotifier
from .whatsapp import WhatsAppNotifier

_NOTIFIERS: dict[str, type[Notifier]] = {n.key: n for n in (WhatsAppNotifier, TelegramNotifier, DiscordNotifier, SignalNotifier)}


def available() -> dict[str, type[Notifier]]:
    return dict(_NOTIFIERS)


def describe() -> list[dict]:
    return [
        {"key": n.key, "label": n.label, "config_fields": n.config_fields,
         "config_example": n.config_example}
        for n in _NOTIFIERS.values()
    ]


def get_notifier(kind: str, config: dict | None = None) -> Notifier:
    cls = _NOTIFIERS.get(kind)
    if cls is None:
        raise NotifierError(f"Unknown notifier: {kind}")
    return cls(config=config or {})


__all__ = ["available", "describe", "get_notifier", "Notifier", "NotifierError"]
