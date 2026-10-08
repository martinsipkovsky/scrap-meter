"""Signal notifier: alerts into Signal groups (or to phone numbers).

Signal has no bot API, so the app talks to signal-cli-rest-api
(github.com/bbernhard/signal-cli-rest-api), a small service that runs next
to it as its own container (deploy/docker-compose.signal.yml). The service
is linked to a phone as a secondary device, like Signal Desktop: the QR code
is shown on the Notifications page and scanned in Signal → Settings →
Linked devices. Alerts are then sent from that phone's number, and the app
reads the groups the phone is in, for chat commands and the Chat room.

The service's address is SIGNAL_API_URL (e.g. http://signal:8080); without
it Signal is not set up. Run the service in MODE=native (the compose file
does), so it answers fast.

Config of a provider: {"to": ["group.abc...=", "+421900123456"]}: group ids
from the group list on the Notifications page, or phone numbers with
country code.
"""
from __future__ import annotations

import threading
import time

import httpx

from ..config import settings
from .base import Notifier, NotifierError, report_sent

_TIMEOUT = 15.0
DEVICE_NAME = "Scrap Meter"


def recipients(value) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    return [str(i).strip() for i in items if str(i).strip()]


def base_url() -> str:
    """The service's address; empty while Signal is off in the Developer options."""
    from .. import dev_options

    if not dev_options.enabled("signal"):
        return ""
    return (settings.signal_api_url or "").strip().rstrip("/")


def _request(method: str, path: str, body: dict | None = None, params: dict | None = None,
             timeout: float = _TIMEOUT) -> httpx.Response:
    url = base_url()
    if not url:
        from .. import dev_options

        if not dev_options.enabled("signal"):
            raise NotifierError("Signal is off (Settings → Developer options)")
        raise NotifierError("Signal is not set up: run the signal-cli-rest-api service and set SIGNAL_API_URL "
                            "(see deploy/docker-compose.signal.yml)")
    try:
        resp = httpx.request(method, url + path, json=body, params=params, timeout=timeout)
    except httpx.HTTPError as exc:
        raise NotifierError(f"cannot reach the Signal service at {url}: {exc.__class__.__name__}") from exc
    if resp.status_code >= 400:
        try:
            desc = resp.json().get("error")
        except (ValueError, AttributeError):
            desc = None
        raise NotifierError(f"signal: {desc or 'HTTP ' + str(resp.status_code)}")
    return resp


def _json(resp: httpx.Response):
    try:
        return resp.json()
    except ValueError:
        return None


# ---- the linked number -----------------------------------------------------------
_account: dict = {"number": None, "checked": 0.0}
_account_lock = threading.Lock()


def number(refresh: bool = False) -> str | None:
    """The phone number the service is linked to (checked at most every 30 s)."""
    with _account_lock:
        if refresh or time.monotonic() - _account["checked"] > 30:
            _account["checked"] = time.monotonic()
            accounts = _json(_request("GET", "/v1/accounts")) or []
            _account["number"] = accounts[0] if accounts else None
        return _account["number"]


def status() -> dict:
    """{"state": off | not_set_up | unreachable | not_linked | linked, "number", "error"}"""
    from .. import dev_options

    if not dev_options.enabled("signal"):
        return {"state": "off", "number": None, "error": None}
    if not base_url():
        return {"state": "not_set_up", "number": None, "error": None}
    try:
        n = number(refresh=True)
    except NotifierError as exc:
        return {"state": "unreachable", "number": None, "error": str(exc)}
    return {"state": "linked" if n else "not_linked", "number": n, "error": None}


def qr_png() -> bytes:
    """A QR code to link the service to a phone (valid until scanned or for a minute or two)."""
    return _request("GET", "/v1/qrcodelink", params={"device_name": DEVICE_NAME}, timeout=30).content


def _linked() -> str:
    n = number()
    if not n:
        raise NotifierError("signal: no phone is linked (link one on the Notifications page)")
    return n


# ---- groups ------------------------------------------------------------------------
_groups: dict = {"rows": [], "at": 0.0}


def groups(refresh: bool = False) -> list[dict]:
    """The linked phone's groups: [{"id": "group.…", "internal_id", "name"}] (cached for a minute)."""
    if refresh or time.monotonic() - _groups["at"] > 60:
        rows = _json(_request("GET", f"/v1/groups/{_linked()}")) or []
        _groups["rows"] = [{"id": g.get("id"), "internal_id": g.get("internal_id"), "name": g.get("name") or g.get("id")}
                           for g in rows if g.get("id")]
        _groups["at"] = time.monotonic()
    return list(_groups["rows"])


def group_by_internal(internal_id: str) -> dict | None:
    for refresh in (False, True):
        g = next((g for g in groups(refresh) if g["internal_id"] == internal_id), None)
        if g is not None:
            return g
    return None


# ---- sending and receiving ------------------------------------------------------------
def send_to(to: str, text: str, report: bool = True) -> None:
    """Send one message to a group id or phone number; report=False keeps it
    from the Chat room hook (the Chat room records what it sends itself)."""
    _request("POST", "/v2/send", {"message": text, "number": _linked(), "recipients": [to]})
    if report:
        report_sent("signal", str(to), text)


def receive() -> list[dict]:
    """Group messages that arrived since the last call:
    [{"chat": group id, "chat_name", "sender", "sender_name", "text", "ts", "id"}]."""
    n = number()
    if not n:
        return []
    rows = _json(_request("GET", f"/v1/receive/{n}", params={"timeout": 1}, timeout=30)) or []
    out = []
    for row in rows:
        env = (row or {}).get("envelope") or {}
        data = env.get("dataMessage") or ((env.get("syncMessage") or {}).get("sentMessage")) or {}
        info = data.get("groupInfo") or {}
        text = data.get("message")
        if not info.get("groupId") or not text:
            continue  # private messages and receipts, reactions, ...
        g = group_by_internal(info["groupId"])
        if g is None:
            continue
        ts = data.get("timestamp") or env.get("timestamp")
        out.append({"chat": g["id"], "chat_name": g["name"], "sender": env.get("source") or env.get("sourceNumber") or n,
                    "sender_name": env.get("sourceName") or env.get("source") or n, "text": text,
                    "ts": ts / 1000 if ts else time.time(), "id": f"{env.get('source')}:{ts}"})
    return out


class SignalNotifier(Notifier):
    key = "signal"
    label = "Signal"
    config_fields = {
        "to": "group id (group.…, see the Signal group list above) or phone number with country code; a list for several",
    }
    config_example = {"to": ["group.abc123="]}

    def send(self, message: str) -> None:
        to = recipients(self.config.get("to"))
        if not to:
            raise NotifierError("signal requires at least one group id or phone number in to")
        errors = []
        for r in to:
            try:
                send_to(r, message)
            except NotifierError as exc:
                errors.append(f"{r}: {exc}")
        if errors:
            raise NotifierError("signal: " + "; ".join(errors))
