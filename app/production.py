"""Production state of a station: running, idle (auto-stopped) or stopped.

* running  - the OK counter increased within the station's idle timeout.
* idle     - no pass increase for idle_timeout_min minutes. The line is most
             likely not producing, so NOK counts are treated as false signals
             (dashboard grays the station, scrap alerts are suppressed). It goes
             back to running by itself as soon as the pass counter increases.
* stopped  - an operator pressed Stop. Stays stopped until Start is pressed,
             even if counts keep arriving.

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
    """Called by the poller whenever the pass counter went up."""
    station.last_pass_change_at = now or utcnow()


def start(station: Station) -> None:
    station.manual_stop = False
    station.last_pass_change_at = utcnow()


def stop(station: Station) -> None:
    station.manual_stop = True
