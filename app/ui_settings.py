"""Look and behaviour of the web pages (the Settings tab).

Every user can choose the mode (dark, light or as the system), the colour of
buttons and links, density, text size, date and time format, the page shown
after signing in, how often the dashboard refreshes, whether its sound is on
and whether the menu starts folded. What a user leaves at "default" follows
the defaults an administrator sets for everyone (kept with settings_store,
key ui_defaults), and those follow the built-in ones below.

A user's own choices are User.ui_settings ({key: value}, only the keys they
set). Sound and the folded menu also remember the last switch in each browser;
the setting is what a browser starts with.
"""
from __future__ import annotations

from typing import Any

from . import settings_store
from .models import User

DEFAULTS_KEY = "ui_defaults"

# key: (built-in default, allowed values, label)
FIELDS: dict[str, tuple[Any, tuple, str]] = {
    "mode": ("dark", ("dark", "light", "system"), "Mode"),
    "accent": ("blue", ("blue", "green", "purple", "orange", "teal"), "Colour scheme"),
    "density": ("comfortable", ("comfortable", "compact"), "Density"),
    "text_size": ("normal", ("small", "normal", "large"), "Text size"),
    "date_format": ("dmy", ("dmy", "ymd", "mdy", "dmy_slash"), "Date format"),
    "time_format": ("24", ("24", "12"), "Time format"),
    "start_page": ("/", ("/", "/stations", "/devices", "/jobs", "/data", "/scrap", "/chat"), "Start page"),
    "refresh_s": (5, (2, 5, 10, 30, 60), "Dashboard refresh"),
    "sound": ("on", ("on", "off"), "Dashboard sound"),
    "menu": ("open", ("open", "folded"), "Menu"),
}

# the permission a start page needs (the dashboard otherwise)
START_PAGE_PERMISSION = {"/": "view_dashboard", "/stations": "view_dashboard", "/devices": "view_dashboard",
                         "/jobs": "view_dashboard", "/data": "view_data", "/scrap": "view_data", "/chat": "chat_room"}

_defaults_cache: dict | None = None


def clean(values: Any) -> dict:
    """Only known keys with allowed values (others are dropped)."""
    out = {}
    if not isinstance(values, dict):
        return out
    for key, value in values.items():
        if key not in FIELDS:
            continue
        allowed = FIELDS[key][1]
        if isinstance(allowed[0], int):
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
        if value in allowed:
            out[key] = value
    return out


def builtin() -> dict:
    return {k: f[0] for k, f in FIELDS.items()}


def stored_defaults() -> dict:
    """The administrator's defaults (only what they changed)."""
    global _defaults_cache
    if _defaults_cache is None:
        _defaults_cache = clean(settings_store.load(DEFAULTS_KEY))
    return dict(_defaults_cache)


def defaults() -> dict:
    return {**builtin(), **stored_defaults()}


def save_defaults(values: dict) -> dict:
    global _defaults_cache
    cleaned = clean(values)
    settings_store.save(DEFAULTS_KEY, cleaned or None)
    _defaults_cache = cleaned
    return cleaned


def effective(user: User | None) -> dict:
    """The settings a page uses for this user."""
    return {**defaults(), **(clean(user.ui_settings) if user is not None else {})}


def start_page(user: User) -> str:
    page = effective(user)["start_page"]
    return page if user.has_permission(START_PAGE_PERMISSION.get(page, "view_dashboard")) else "/"


def choices() -> dict:
    return {k: {"label": f[2], "values": list(f[1]), "builtin": f[0]} for k, f in FIELDS.items()}
