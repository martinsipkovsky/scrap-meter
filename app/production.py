"""Production state of a station: running, idle (auto-stopped) or stopped.

* running  - a source's start rule fired (``start_count`` OK pieces within
             ``start_window_s`` seconds, 2 within 60 by default), or Start was
             pressed, and since then some source's OK counter has increased
             within the station's idle timeout.
* idle     - no OK increase for idle_timeout_min minutes. The line is most
             likely not producing, so counter changes are false signals: they
             are not counted (app.stations), the dashboard grays the station
             and scrap alerts are suppressed. It goes back to running by
             itself when a source's start rule fires again.
* stopped  - an operator pressed Stop. Stays stopped, and counts nothing,
             until Start is pressed, even if counts keep arriving.

Start clears a manual stop and restarts the idle clock, so a station that
still does not count will fall back to idle after its timeout.
"""
from __future__ import annotations

import datetime as dt

from .models import Station, utcnow

RUNNING = "running"
IDLE = "idle"
STOPPED = "stopped"

DEFAULT_IDLE_TIMEOUT_MIN = 30
# a source's start rule: this many OK pieces within this many seconds
DEFAULT_START_COUNT = 2
DEFAULT_START_WINDOW_S = 60


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


def state(station: Station, now: dt.datetime | None = None) -> str:
    if station.manual_stop:
        return STOPPED
    last = _aware(station.last_pass_change_at)
    if last is None:
        return IDLE
    now = now or utcnow()
    timeout = max(1, station.idle_timeout_min or DEFAULT_IDLE_TIMEOUT_MIN)
    if (now - last).total_seconds() >= timeout * 60:
        return IDLE
    return RUNNING


def in_production(station: Station, now: dt.datetime | None = None) -> bool:
    return state(station, now) == RUNNING


def describe(station: Station, now: dt.datetime | None = None) -> dict:
    """Fields the API returns about a station's production state."""
    return {
        "production_state": state(station, now),
        "in_production": in_production(station, now),
        "manual_stop": bool(station.manual_stop),
        "idle_timeout_min": station.idle_timeout_min or DEFAULT_IDLE_TIMEOUT_MIN,
        "last_pass_change_at": _aware(station.last_pass_change_at),
    }


def note_pass_increase(station: Station, now: dt.datetime | None = None) -> None:
    """A source's OK counter went up while counted, or its start rule fired."""
    station.last_pass_change_at = now or utcnow()


def start(station: Station) -> None:
    station.manual_stop = False
    station.last_pass_change_at = utcnow()


def stop(station: Station) -> None:
    station.manual_stop = True
