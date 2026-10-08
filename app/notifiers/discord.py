"""Discord notifier: alerts into Discord channels.

Two modes (``mode`` in the provider's config):

* ``bot`` (default): a Discord bot posts into channels of a server and reads
  them, so chat commands ("!status", "!mute Line 1") and the Chat room work
  in those channels too.
    Config: {"mode": "bot", "bot_token": "MTIz...", "channel_ids": ["1234567890123456789"]}
  Setup, once: in the Discord developer portal (discord.com/developers)
  create an application, add a Bot, copy its token, and turn on
  **Message Content Intent** (Bot page, Privileged Gateway Intents);
  without it the bot gets messages without their text and can't answer
  commands. Invite the bot to the server (OAuth2 → URL generator, scope
  "bot", permissions View Channels, Send Messages, Read Message History).
  A channel id is copied with right click → Copy Channel ID (turn on
  Developer Mode in Discord's Advanced settings first).
* ``webhook``: only sends, no bot needed. In the channel's settings →
  Integrations → Webhooks → New Webhook → Copy Webhook URL.
    Config: {"mode": "webhook", "webhook_url": "https://discord.com/api/webhooks/..."}

The app reads a bot's channels by asking Discord for new messages every few
seconds (app.messengers); it needs no open connection to Discord.
"""
from __future__ import annotations

import httpx

from .base import Notifier, NotifierError, report_sent

_TIMEOUT = 10.0
API = "https://discord.com/api/v10"
MAX_LEN = 2000  # Discord's limit for one message


def channel_list(value) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    return [str(i).strip() for i in items if str(i).strip()]


def mode(config: dict) -> str:
    return "webhook" if (config.get("mode") == "webhook" or (config.get("webhook_url") and not config.get("bot_token"))) \
        else "bot"


def chunks(text: str, size: int = MAX_LEN) -> list[str]:
    """Split a long message at line ends (Discord takes 2000 characters)."""
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > size:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:size])
            line = line[size:]
        if cur and len(cur) + 1 + len(line) > size:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur or not out:
        out.append(cur)
    return out


class DiscordNotifier(Notifier):
    key = "discord"
    label = "Discord"
    config_fields = {
        "mode": "'bot' (sends, and reads commands and the Chat room) or 'webhook' (only sends)",
        "bot_token": "bot: the bot's token from the Discord developer portal. Turn on Message Content Intent "
                     "there, or the bot can't read commands",
        "channel_ids": "bot: list of channel ids (right click a channel → Copy Channel ID, with Developer Mode on)",
        "webhook_url": "webhook: the channel's webhook URL (channel settings → Integrations → Webhooks)",
    }
    config_example = {"mode": "bot", "bot_token": "MTIz...", "channel_ids": ["1234567890123456789"]}

    def send(self, message: str) -> None:
        if mode(self.config) == "webhook":
            url = (self.config.get("webhook_url") or "").strip()
            if not url:
                raise NotifierError("discord webhook mode requires webhook_url")
            for part in chunks(message):
                _request("POST", url, None, {"content": part})
            return
        channels = channel_list(self.config.get("channel_ids") or self.config.get("channel_id"))
        if not (self.config.get("bot_token") or "").strip() or not channels:
            raise NotifierError("discord requires bot_token and at least one channel id in channel_ids")
        errors = []
        for channel in channels:
            try:
                send_to(self.config, channel, message)
            except ChannelError as exc:
                errors.append(f"channel {channel}: {exc}")
        if errors:
            raise NotifierError("discord: " + "; ".join(errors))


class ChannelError(NotifierError):
    """Discord refused one channel (the others can still work)."""


def _request(method: str, url: str, token: str | None, body: dict | None = None, params: dict | None = None):
    headers = {"Authorization": f"Bot {token}"} if token else {}
    try:
        resp = httpx.request(method, url, json=body, params=params, headers=headers, timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        raise NotifierError(f"cannot reach Discord: {exc.__class__.__name__}") from exc
    if resp.status_code == 204:
        return None
    try:
        data = resp.json()
    except ValueError:
        data = None
    if resp.status_code >= 400:
        desc = (data or {}).get("message") if isinstance(data, dict) else None
        desc = desc or f"HTTP {resp.status_code}"
        if resp.status_code == 401:
            raise NotifierError(f"discord: {desc}. Check the bot token.")
        if resp.status_code == 403:
            raise ChannelError(f"{desc} (the bot has no access to this channel)")
        if resp.status_code == 404 and token is None:
            raise NotifierError(f"discord: {desc}. Check the webhook URL.")
        raise ChannelError(desc)
    return data


def call(config: dict, method: str, path: str, body: dict | None = None, params: dict | None = None):
    """One call of the bot API (``path`` like "/channels/123/messages")."""
    token = (config.get("bot_token") or "").strip()
    if not token:
        raise NotifierError("discord requires bot_token")
    base = (config.get("base_url") or API).rstrip("/")
    return _request(method, base + path, token, body, params)


def send_to(config: dict, channel: str, text: str, report: bool = True) -> None:
    """Send one message to one channel; report=False keeps it from the Chat
    room hook (the Chat room records the messages it sends itself)."""
    for part in chunks(text):
        call(config, "POST", f"/channels/{channel}/messages", {"content": part})
    if report:
        report_sent("discord", str(channel), text)


def channel_name(config: dict, channel: str) -> str:
    info = call(config, "GET", f"/channels/{channel}") or {}
    return ("#" + info["name"]) if info.get("name") else channel


def bot_user(config: dict) -> dict:
    return call(config, "GET", "/users/@me") or {}


def new_messages(config: dict, channel: str, after: str | None) -> list[dict]:
    """Messages after a message id, oldest first (the latest one when
    ``after`` is None, to start from)."""
    params = {"limit": 50 if after else 1}
    if after:
        params["after"] = after
    rows = call(config, "GET", f"/channels/{channel}/messages", params=params) or []
    return sorted(rows, key=lambda m: int(m.get("id", 0)))
