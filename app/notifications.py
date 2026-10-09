"""Notification rules: what is sent, how urgent it is, and where it goes.

Each rule has a condition (one of CONDITIONS), a severity (info / warning /
alert, shown in front of the message) and the providers that receive it (none
selected = every enabled provider, which is how rules made before this choice
existed keep working). A provider is one destination: a WhatsApp group, a
Telegram chat, a webhook.

Two kinds of condition:

* state conditions (scrap_rate, fail_count, disconnected) are checked by
  ``evaluate_station`` after each reading or failed read, and fire again after
  the rule's cooldown for as long as they hold;
* event conditions (production_change, job_change, backup results,
  app_started) fire once when the thing happens, through ``emit``;
* slow_response is checked after every ping round (app.ping, ``check_ping``):
  a device of the station answered its last 3 pings slower than the
  threshold (ms) or not at all. Sent again after the cooldown while it lasts,
  and one "back to normal" message when a ping is fast again.

Rules for all stations can override their threshold per station
(rule.thresholds = {"<station id>": value}). A station whose alerts are muted
(app.mute) gets none.

With "Send alerts only when the station is in production" on (the default,
settings_store key alert_policy), alerts about a station that is idle or
stopped are not sent but written to the alert log as skipped, at most once per
rule cooldown. production_change itself is still sent: it is the message that
says the station left or rejoined production. Chat commands answer regardless.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading

from sqlalchemy.orm import Session

from . import notifiers, production, settings_store
from .models import (
    NotificationLog,
    NotificationProvider,
    NotificationRule,
    Station,
    utcnow,
)

log = logging.getLogger("cognex.notifications")

# key -> how the Notifications page offers it
CONDITIONS: dict[str, dict] = {
    "scrap_rate": {"label": "Scrap rate ≥ threshold", "group": "Production",
                   "severity": "alert", "threshold": "fraction, e.g. 0.05 = 5%", "station": True},
    "fail_count": {"label": "Fail (NOK) count ≥ threshold", "group": "Production",
                   "severity": "warning", "threshold": "parts", "station": True},
    "disconnected": {"label": "Station's device disconnected or read error", "group": "Faults",
                     "severity": "alert", "station": True},
    "slow_response": {"label": "Device slow or not answering pings", "group": "Faults",
                      "severity": "warning", "threshold": "ms", "default_threshold": 300, "station": True,
                      "needs": "ping"},
    "production_change": {"label": "Station stopped, idle or back in production", "group": "Production",
                          "severity": "warning", "station": True, "event": True},
    "job_change": {"label": "Station changed job", "group": "Production",
                   "severity": "info", "station": True, "event": True},
    "backup_failed": {"label": "FTP backup failed", "group": "System", "severity": "alert", "event": True},
    "backup_ok": {"label": "FTP backup finished", "group": "System", "severity": "info", "event": True},
    "app_started": {"label": "App started or updated", "group": "System", "severity": "info", "event": True},
}
SEVERITIES = {"info": "ℹ️ INFO", "warning": "⚠️ WARNING", "alert": "🚨 ALERT"}

# app_started is sent this long after startup, so the WhatsApp client has
# reconnected by then
STARTUP_NOTICE_DELAY = 60

POLICY_KEY = "alert_policy"
# event conditions sent whatever the station's production state
_ALWAYS_SENT = {"production_change"}
_policy: dict | None = None
# (rule id, station id) -> when a skipped alert was last written to the log
_skip_logged: dict[tuple[int, int], dt.datetime] = {}


def policy() -> dict:
    """{"production_only": bool}; on when never saved (also on an upgrade)."""
    global _policy
    if _policy is None:
        stored = settings_store.load(POLICY_KEY) or {}
        _policy = {"production_only": bool(stored.get("production_only", True))}
    return dict(_policy)


def save_policy(values: dict) -> dict:
    global _policy
    new = {"production_only": bool(values.get("production_only", policy()["production_only"]))}
    settings_store.save(POLICY_KEY, new)
    _policy = new
    return dict(new)


def _held_back(station: Station | None, condition: str) -> bool:
    """Not sent: the station is not in production and alerts go out only then."""
    return (station is not None and condition not in _ALWAYS_SENT
            and policy()["production_only"] and not production.in_production(station))


def _skip(db: Session, rule: NotificationRule, station: Station, message: str, now: dt.datetime) -> None:
    """Log a held-back alert as skipped, once per rule cooldown (a device that
    stays offline is checked after every failed read)."""
    key = (rule.id, station.id)
    last = _skip_logged.get(key)
    gap = rule.cooldown or 0
    if not CONDITIONS.get(rule.condition, {}).get("event"):
        gap = max(gap, 60)
    if last is not None and (now - last).total_seconds() < gap:
        return
    _skip_logged[key] = now
    state = production.state(station)
    db.add(NotificationLog(
        rule_id=rule.id, message=format_message(message, rule.severity), delivered=False, skipped=True,
        detail=f"Not sent: station '{station.name}' is not in production ({state}); "
               "alerts go out only during production (Notifications settings)"))
    db.commit()


def _cooldown_ok(rule: NotificationRule, now: dt.datetime) -> bool:
    if rule.last_fired_at is None:
        return True
    last = rule.last_fired_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=dt.timezone.utc)
    return (now - last).total_seconds() >= rule.cooldown


def threshold_for(rule: NotificationRule, station: Station) -> float:
    """The rule's threshold, or the station's override on a rule for all stations."""
    if rule.station_id is None and rule.thresholds:
        value = rule.thresholds.get(str(station.id))
        if value is not None and value != "":
            return float(value)
    if rule.condition == "slow_response" and not rule.threshold:
        return float(CONDITIONS["slow_response"]["default_threshold"])
    return rule.threshold


def _condition_met(rule: NotificationRule, station: Station, state,
                   online: dict) -> tuple[bool, str]:
    if rule.condition == "disconnected":
        if not online["connected"]:
            return True, f"Station '{station.name}' is disconnected: {online['problem'] or 'no data'}"
        return False, ""

    if state is None:
        return False, ""

    # A station that is not in production (idle or stopped by an operator)
    # only produces false NOK signals, so counter-based alerts are not sent.
    if not production.in_production(station):
        return False, ""

    threshold = threshold_for(rule, station)
    # Counter alerts use the counters shown on the dashboard, so a reset there
    # also clears the condition.
    if rule.condition == "scrap_rate":
        if state.shown_count > 0 and state.shown_scrap_rate >= threshold:
            pct = state.shown_scrap_rate * 100
            return True, (
                f"High scrap on '{station.name}' job '{state.job_name}': "
                f"{pct:.1f}% ({state.shown_fail}/{state.shown_count}) "
                f">= {threshold * 100:.1f}%"
            )
        return False, ""

    if rule.condition == "fail_count":
        if state.shown_fail >= threshold:
            return True, (
                f"Fail count on '{station.name}' job '{state.job_name}' "
                f"reached {state.shown_fail} (>= {int(threshold)})"
            )
        return False, ""

    return False, ""


def format_message(message: str, severity: str | None) -> str:
    label = SEVERITIES.get(severity or "")
    return f"{label} · {message}" if label else message


def dispatch(
    db: Session,
    message: str,
    rule_id: int | None = None,
    provider_ids: list[int] | None = None,
    severity: str | None = None,
) -> None:
    """Send one message to the given providers (all enabled ones when none
    are given) and log each result."""
    message = format_message(message, severity)
    q = db.query(NotificationProvider).filter(NotificationProvider.enabled.is_(True))
    if provider_ids:
        q = q.filter(NotificationProvider.id.in_([int(i) for i in provider_ids]))
    providers = q.all()
    if not providers:
        detail = "the rule's providers are deleted or disabled" if provider_ids else "no enabled providers"
        db.add(NotificationLog(rule_id=rule_id, message=message, delivered=False, detail=detail))
        db.commit()
        return

    for provider in providers:
        detail = "ok"
        delivered = True
        try:
            notifier = notifiers.get_notifier(provider.kind, provider.config)
            notifier.send(message)
        except Exception as exc:  # noqa: BLE001 - record every failure
            delivered = False
            detail = f"{provider.name}: {exc}"
        db.add(
            NotificationLog(
                rule_id=rule_id,
                message=message,
                delivered=delivered,
                detail=f"{provider.name}: {detail}" if delivered else detail,
            )
        )
    db.commit()


def _fire(db: Session, rule: NotificationRule, message: str, now: dt.datetime) -> None:
    dispatch(db, message, rule_id=rule.id, provider_ids=rule.provider_ids, severity=rule.severity)
    rule.last_fired_at = now
    db.commit()


def _rules(db: Session, conditions: list[str], station: Station | None) -> list[NotificationRule]:
    q = db.query(NotificationRule).filter(
        NotificationRule.enabled.is_(True), NotificationRule.condition.in_(conditions)
    )
    if station is not None:
        q = q.filter((NotificationRule.station_id == station.id) | (NotificationRule.station_id.is_(None)))
    return q.all()


def evaluate_station(db: Session, station: Station) -> None:
    """Check the state rules that apply to a station and fire the ones that match."""
    from . import stations

    now = utcnow()
    check_production(db, station)
    if station.alerts_muted:  # muted with "!mute" or the station view's switch (app.mute)
        return
    active_state = stations.shown(db, station)
    online = stations.status(station, stations.devices_of(db, [station]))
    for rule in _rules(db, ["scrap_rate", "fail_count", "disconnected"], station):
        met, message = _condition_met(rule, station, active_state, online)
        if met and _held_back(station, rule.condition):
            _skip(db, rule, station, message, now)
        elif met and _cooldown_ok(rule, now):
            _fire(db, rule, message, now)


def emit(db: Session, condition: str, message: str, station: Station | None = None) -> None:
    """An event happened: send it through every enabled rule for it (not for a
    station whose alerts are muted)."""
    if station is not None and station.alerts_muted:
        return
    now = utcnow()
    held_back = _held_back(station, condition)
    for rule in _rules(db, [condition], station):
        if held_back:
            _skip(db, rule, station, message, now)
        elif _cooldown_ok(rule, now):
            _fire(db, rule, message, now)


def emit_system(condition: str, message: str) -> None:
    """emit() with its own database session, for code outside a request."""
    from .database import SessionLocal

    db = SessionLocal()
    try:
        emit(db, condition, message)
    except Exception:  # noqa: BLE001 - a notification must never break the caller
        db.rollback()
        log.exception("could not send the %s notification", condition)
    finally:
        db.close()


_PRODUCTION_TEXT = {
    production.RUNNING: "is back in production",
    production.IDLE: "is not in production (no passes for {timeout} min)",
    production.STOPPED: "was stopped by an operator",
}


def check_production(db: Session, station: Station) -> None:
    """Send production_change when the station's production state changed
    since the last check. The first check only records the state."""
    current = production.state(station)
    previous = station.notified_state
    if previous == current:
        return
    station.notified_state = current
    db.commit()
    if previous is None:
        return
    text = _PRODUCTION_TEXT[current].format(timeout=station.idle_timeout_min or production.DEFAULT_IDLE_TIMEOUT_MIN)
    emit(db, "production_change", f"Station '{station.name}' {text}", station)


# (rule id, station id, device id) -> when the slow-response alert was last sent
_ping_alerted: dict[tuple[int, int, int], dt.datetime] = {}


def _ms(v) -> str:
    return "no reply" if v is None else f"{v:.0f} ms"


def check_ping(db: Session) -> None:
    """After a ping round: slow_response rules, per station and device."""
    from . import ping, stations

    now = utcnow()
    rules = _rules(db, ["slow_response"], None)
    if not rules:
        _ping_alerted.clear()
        return
    all_stations = db.query(Station).all()
    devices = stations.devices_of(db, all_stations)
    for rule in rules:
        for station in all_stations:
            if rule.station_id is not None and rule.station_id != station.id:
                continue
            thr = threshold_for(rule, station)
            for device_id in sorted(station.device_ids()):
                device = devices.get(device_id)
                recent = ping.recent(device_id)
                if device is None or not recent:
                    continue
                key = (rule.id, station.id, device_id)
                last3 = recent[-ping.HISTORY:]
                bad = len(last3) == ping.HISTORY and all(v is None or v >= thr for v in last3)
                who = f"Device '{device.name}' (station '{station.name}')"
                if bad:
                    sent = _ping_alerted.get(key)
                    if sent is not None and (now - sent).total_seconds() < (rule.cooldown or 0):
                        continue
                    if all(v is None for v in last3):
                        message = f"{who} does not answer pings ({ping.HISTORY} in a row)"
                    else:
                        message = (f"{who} responds slowly: last pings {', '.join(_ms(v) for v in last3)} "
                                   f"(>= {thr:.0f} ms)")
                elif key in _ping_alerted and last3[-1] is not None and last3[-1] < thr:
                    message = f"{who} responds normally again: {_ms(last3[-1])}"
                else:
                    continue
                if not bad:
                    _ping_alerted.pop(key, None)  # recovered: "back to normal" follows a sent alert only
                if station.alerts_muted:
                    continue
                if _held_back(station, rule.condition):
                    _skip(db, rule, station, message, now)
                    continue
                dispatch(db, message, rule_id=rule.id, provider_ids=rule.provider_ids, severity=rule.severity)
                rule.last_fired_at = now
                db.commit()
                if bad:
                    _ping_alerted[key] = now


def check_all_production(db: Session) -> None:
    """Called periodically: a station goes idle by time, with no reading."""
    for station in db.query(Station).all():
        check_production(db, station)


def startup_notice(version: str, previous: str | None, delay: float = STARTUP_NOTICE_DELAY) -> None:
    if previous and previous != version:
        message = f"Scrap Meter was updated from version {previous} to {version} and is running."
    else:
        message = f"Scrap Meter {version} started."
    timer = threading.Timer(delay, emit_system, args=("app_started", message))
    timer.daemon = True
    timer.start()
