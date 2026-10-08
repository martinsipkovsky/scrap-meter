"""HMI windows: device web pages (a camera's WebHMI, a PLC's web server)
shown on a station view.

The viewer's browser loads each page straight from the device in an iframe,
so the browser must reach the device's address, and the device must allow
being shown inside another page. Many devices forbid that with an
``X-Frame-Options`` header or a ``Content-Security-Policy: frame-ancestors``
rule; the browser then shows an empty or "refused to connect" frame and,
by design, tells the page nothing about it. ``check`` asks the device from
the server and reads those headers, so the station view can say why a window
stays empty. It only reports what the server sees: a device the server can't
reach may still be reachable from the viewer's browser, and the other way
round.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

import httpx

DEFAULT_HEIGHT = 600
CHECK_TIMEOUT_S = 5.0
# a scheme ("javascript:", "https:"), not a host and port ("10.0.0.5:8080")
_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):(?!\d)")


def check_url(url: str) -> str:
    """The URL to store for an HMI window: http(s) only, with a host. An
    address without a scheme ("10.0.0.5/hmi") gets http://."""
    url = url.strip()
    scheme = _SCHEME.match(url)
    if scheme and scheme.group(1).lower() not in ("http", "https"):
        raise ValueError("must be an http:// or https:// address")
    if not scheme:
        url = "http://" + url
    parts = urlsplit(url)
    try:
        parts.port  # noqa: B018 - raises on a bad port
    except ValueError as exc:
        raise ValueError("has a bad port number") from exc
    if not parts.hostname or parts.hostname in ("http", "https"):
        raise ValueError("must name a device, e.g. http://192.168.0.10/")
    return url


def windows(st) -> list[dict]:
    """The station's HMI windows as stored ([] when it has none)."""
    return [dict(w) for w in (st.hmi_windows or [])]


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    port = parts.port or {"http": 80, "https": 443}.get(scheme)
    return scheme, (parts.hostname or "").lower(), port


def _source_matches(source: str, app_origin: str, device_url: str) -> bool:
    """Whether one CSP frame-ancestors source allows ``app_origin``."""
    source = source.strip().lower()
    a_scheme, a_host, a_port = _origin(app_origin)
    if source == "*":
        return True
    if source == "'self'":
        return _origin(device_url) == (a_scheme, a_host, a_port)
    if source in ("http:", "https:"):
        return a_scheme == source[:-1] or (source == "http:" and a_scheme == "https")
    if source.startswith("'"):
        return False  # 'none' and keywords that don't apply here
    if "://" not in source:
        source = a_scheme + "://" + source
    source = source.rstrip("/")
    any_port = source.endswith(":*")
    if any_port:
        source = source[:-2]
    try:
        s_scheme, s_host, s_port = _origin(source)
    except ValueError:
        return False
    if s_scheme != a_scheme:
        return False
    if s_host.startswith("*."):
        if not a_host.endswith(s_host[1:]):
            return False
    elif s_host != a_host:
        return False
    return any_port or s_port == a_port


def framing(headers, app_origin: str, device_url: str) -> str | None:
    """Why the browser won't show the page inside the app (None: it may).

    A CSP frame-ancestors rule wins over X-Frame-Options, as in browsers.
    """
    headers = httpx.Headers(headers)
    for csp in headers.get_list("content-security-policy"):
        for directive in csp.split(";"):
            name, _, value = directive.strip().partition(" ")
            if name.lower() != "frame-ancestors":
                continue
            sources = value.split()
            if any(_source_matches(s, app_origin, device_url) for s in sources):
                return None
            return "the device only allows its page inside " + (
                "no other page" if sources in ([], ["'none'"]) else "these pages: " + " ".join(sources)
            ) + " (Content-Security-Policy frame-ancestors)"
    xfo = (headers.get("x-frame-options") or "").strip().lower()
    if xfo == "deny":
        return "the device forbids showing its page inside another page (X-Frame-Options: DENY)"
    if xfo == "sameorigin" and _origin(device_url) != _origin(app_origin):
        return "the device only allows its page inside its own pages (X-Frame-Options: SAMEORIGIN)"
    return None


async def check(url: str, app_origin: str) -> dict:
    """Ask the device for the page from the server and report whether a
    browser on ``app_origin`` would show it in a window."""
    out = {"reachable": False, "status": None, "blocked": None, "error": None}
    try:
        async with httpx.AsyncClient(verify=False, follow_redirects=True, timeout=CHECK_TIMEOUT_S,
                                     max_redirects=5) as client:
            async with client.stream("GET", url) as resp:
                out.update(reachable=True, status=resp.status_code,
                           blocked=framing(resp.headers, app_origin, str(resp.url)))
    except httpx.TimeoutException:
        out["error"] = "no answer within %d s" % CHECK_TIMEOUT_S
    except httpx.HTTPError as exc:
        out["error"] = str(exc) or exc.__class__.__name__
    return out
