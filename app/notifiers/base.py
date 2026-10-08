"""Notifier contract. Each notifier is one file, like the protocols."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

log = logging.getLogger("cognex.notifiers")


class NotifierError(Exception):
    pass


# called with (kind, chat, text) after a notifier delivered a message to one
# chat (set by app.chatroom, which keeps the messages sent into its chat)
on_sent = None


def report_sent(kind: str, chat: str, text: str) -> None:
    if on_sent is None:
        return
    try:
        on_sent(kind, chat, text)
    except Exception:  # noqa: BLE001 - the message is sent
        log.exception("recording a sent %s message failed", kind)


class Notifier(ABC):
    key: str = ""
    label: str = ""
    config_fields: dict[str, str] = {}
    # pre-filled in the Add provider dialog
    config_example: dict = {}

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    @abstractmethod
    def send(self, message: str) -> None:
        """Deliver a message or raise NotifierError."""
        raise NotImplementedError
