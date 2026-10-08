"""WhatsApp group notifier.

Important reality check about WhatsApp *group* chats:

The official WhatsApp Cloud API (Meta) is designed for 1:1 business messaging
and **cannot post into a normal user group chat**. There is no official API
that sends a message to an arbitrary WhatsApp group. So to notify a group we
must use one of a few practical routes, and this notifier supports the common
ones through a single ``transport`` setting:

* ``transport = "linked"`` (no extra service needed, UNOFFICIAL):
    The app itself is logged in as a linked device of your phone (scan the QR
    code on the Notifications page) and sends from your number. See
    whatsapp_linked.py for how it works and the risk of using it.
    Config: {"transport": "linked", "to": "120363012345678901@g.us"}
    ``to`` is a group id (pick it from the group list on the Notifications
    page) or a phone number with country code; a list sends to several.

* ``transport = "webhook"`` (default):
    POST the message as JSON to any HTTP endpoint you control. This is the
    pluggable path: point it at a self-hosted WhatsApp gateway
    (e.g. whatsapp-web.js / Baileys / WAHA / Green API style services) that
    *is* logged into an account that belongs to the group and can post to it.
    Config: {"transport": "webhook", "url": "...", "headers": {...},
             "payload_key": "message", "extra": {"chatId": "...@g.us"}}

* ``transport = "greenapi"``:
    Use the Green API (a popular third-party WhatsApp HTTP gateway) group-send
    endpoint directly.
    Config: {"transport": "greenapi", "id_instance": "...",
             "api_token": "...", "group_id": "1234567890-123456@g.us",
             "base_url": "https://api.green-api.com"}

* ``transport = "cloud_api"``:
    Meta's official Cloud API. Supported for completeness but only sends to an
    individual phone number, **not** a group. Use it to message an operator's
    number, not a group chat.
    Config: {"transport": "cloud_api", "token": "...",
             "phone_number_id": "...", "to": "<recipient msisdn>"}

All network calls use httpx with a short timeout and surface failures as
NotifierError so the notification log records exactly what happened.
"""
from __future__ import annotations

import httpx

from .base import Notifier, NotifierError
from .whatsapp_linked import link

_TIMEOUT = 10.0


class WhatsAppNotifier(Notifier):
    key = "whatsapp"
    label = "WhatsApp group"
    config_fields = {
        "transport": "'linked' | 'webhook' | 'greenapi' | 'cloud_api'",
        "to": "linked: group id (…@g.us, see the group list above) or phone number with country code; a list for several",
        "url": "webhook: endpoint URL to POST the message to",
        "headers": "webhook: optional dict of HTTP headers (e.g. auth)",
        "payload_key": "webhook: JSON key to place the message under (default 'message')",
        "extra": "webhook: extra JSON fields to merge (e.g. the group chatId)",
        "id_instance": "greenapi: instance id",
        "api_token": "greenapi: API token",
        "group_id": "greenapi: group id like '1234-5678@g.us'",
        "base_url": "greenapi: API base url",
        "token": "cloud_api: Meta access token (note: 1:1 only, not groups)",
        "phone_number_id": "cloud_api: sender phone number id",
        "to": "cloud_api: recipient phone number in E.164",
    }

    config_example = {"transport": "linked", "to": "120363012345678901@g.us"}

    def send(self, message: str) -> None:
        transport = self.config.get("transport", "webhook")
        if transport == "linked":
            self._send_linked(message)
        elif transport == "webhook":
            self._send_webhook(message)
        elif transport == "greenapi":
            self._send_greenapi(message)
        elif transport == "cloud_api":
            self._send_cloud_api(message)
        else:
            raise NotifierError(f"Unknown WhatsApp transport: {transport}")

    def _send_linked(self, message: str) -> None:
        from .. import dev_options

        if not dev_options.enabled("whatsapp_linked"):
            raise NotifierError("the WhatsApp virtual client is off (Settings → Developer options)")
        to = self.config.get("to")
        targets = to if isinstance(to, (list, tuple)) else [to]
        if not any(str(t or "").strip() for t in targets):
            raise NotifierError("linked transport requires 'to' (a group id or phone number)")
        for target in targets:
            link.send(str(target), message)

    def _send_webhook(self, message: str) -> None:
        url = self.config.get("url")
        if not url:
            raise NotifierError("webhook transport requires 'url'")
        payload = dict(self.config.get("extra") or {})
        payload[self.config.get("payload_key", "message")] = message
        headers = self.config.get("headers") or {}
        try:
            resp = httpx.post(url, json=payload, headers=headers, timeout=_TIMEOUT)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise NotifierError(f"webhook delivery failed: {exc}") from exc

    def _send_greenapi(self, message: str) -> None:
        base = self.config.get("base_url", "https://api.green-api.com").rstrip("/")
        idi = self.config.get("id_instance")
        token = self.config.get("api_token")
        group = self.config.get("group_id")
        if not all([idi, token, group]):
            raise NotifierError("greenapi transport requires id_instance, api_token and group_id")
        url = f"{base}/waInstance{idi}/sendMessage/{token}"
        try:
            resp = httpx.post(url, json={"chatId": group, "message": message}, timeout=_TIMEOUT)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise NotifierError(f"greenapi delivery failed: {exc}") from exc

    def _send_cloud_api(self, message: str) -> None:
        token = self.config.get("token")
        pnid = self.config.get("phone_number_id")
        to = self.config.get("to")
        if not all([token, pnid, to]):
            raise NotifierError("cloud_api transport requires token, phone_number_id and to")
        url = f"https://graph.facebook.com/v21.0/{pnid}/messages"
        body = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": message},
        }
        try:
            resp = httpx.post(
                url, json=body, headers={"Authorization": f"Bearer {token}"}, timeout=_TIMEOUT
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise NotifierError(f"cloud_api delivery failed: {exc}") from exc
