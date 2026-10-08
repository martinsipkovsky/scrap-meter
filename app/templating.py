"""Shared Jinja2 templates object."""
from __future__ import annotations

import datetime as dt
import hashlib
from functools import lru_cache
from pathlib import Path

from fastapi.templating import Jinja2Templates

from . import __version__

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _fmt_dt(value: dt.datetime | None) -> str:
    if not value:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.strftime("%Y-%m-%d %H:%M:%S UTC")


templates.env.filters["fmt_dt"] = _fmt_dt


@lru_cache(maxsize=None)
def static_url(path: str) -> str:
    """/static/<path>?v=<content hash>, so browsers fetch a new app.js/style.css
    after every update instead of running a cached old copy against new pages."""
    try:
        digest = hashlib.sha1((_STATIC_DIR / path).read_bytes()).hexdigest()[:10]
    except OSError:
        return f"/static/{path}"
    return f"/static/{path}?v={digest}"


templates.env.globals["static_url"] = static_url
# the running version, at the bottom of the left menu (links to the Changelog)
templates.env.globals["app_version"] = __version__
