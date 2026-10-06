"""Notification rules: what is sent, how urgent it is, and where it goes.

Each rule has a condition (one of CONDITIONS), a severity (info / warning /
alert, shown in front of the message) and the providers that receive it (none
selected = every enabled provider, which is how rules made before this choice
existed keep working). A provider is one destination: a WhatsApp group, a
Telegram chat, a webhook.

Two kinds of condition:

* state conditions (scrap_rate, fail_count, disconnected) are checked by
  ``evaluate_device`` after each reading or failed read, and fire again after
  the rule's cooldown for as long as they hold;
* event conditions (production_change, job_change, backup results,
  app_started) fire once when the thing happens, through ``emit``.

Rules for all cameras can override their threshold per camera
(rule.thresholds = {"<device id>": value}).
"""
from __future__ import annotations

import datetime as dt
import logging
import threading

from sqlalchemy.orm import Session

from . import notifiers, production
from .models import (
    CounterState,
    Device,
    NotificationLog,
    NotificationProvider,
    NotificationRule,
    utcnow,
)

log = logging.getLogger("cognex.notifications")

# key -> how the Notifications page offers it
CONDITIONS: dict[str, dict] = {
    "scrap_rate": {"label": "Scrap rate ≥ threshold", "group": "Production",
                   "severity": "alert", "threshold": "fraction, e.g. 0.05 = 5%", "camera": True},
    "fail_count": {"label": "Fail (NOK) count ≥ threshold", "group": "Production",
                   "severity": "warning", "threshold": "parts", "camera": True},
    "disconnected": {"label": "Device disconnected or read error", "group": "Faults",
                     "severity": "alert", "camera": True},
    "production_change": {"label": "Device stopped, idle or back in production", "group": "Production",
                          "severity": "warning", "camera": True, "event": True},
    "job_change": {"label": "Device changed job", "group": "Production",
                   "severity": "info", "camera": True, "event": True},
    "backup_failed": {"label": "FTP backup failed", "group": "System", "severity": "alert", "event": True},
    "backup_ok": {"label": "FTP backup finished", "group": "System", "severity": "info", "event": True},
    "app_started": {"label": "App started or updated", "group": "System", "severity": "info", "event": True},
}
SEVERITIES = {"info": "ℹ️ INFO", "warning": "⚠️ WARNING", "alert": "🚨 ALERT"}

# app_started is sent this long after startup, so the WhatsApp client has
# reconnected by then
STARTUP_NOTICE_DELAY = 60


def _cooldown_ok(rule: NotificationRule, now: dt.datetime) -> bool:
    if rule.last_fired_at is None:
        return True
    last = rule.last_fired_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=dt.timezone.utc)
    return (now - last).total_seconds() >= rule.cooldown


def threshold_for(rule: NotificationRule, device: Device) -> float:
    """The rule's threshold, or the camera's override on a rule for all cameras."""
    if rule.device_id is None and rule.thresholds:
        value = rule.thresholds.get(str(device.id))
        if value is not None and value != "":
            return float(value)
    return rule.threshold


def _condition_met(rule: NotificationRule, device: Device, state: CounterState | None) -> tuple[bool, str]:
    if rule.condition == "disconnected":
        if not device.connected:
            return True, f"Device '{device.name}' is disconnected: {device.last_error or 'no data'}"
        return False, ""

    if state is None:
        return False, ""

    # A camera that is not in production (idle or stopped by an operator) only
    # produces false NOK signals, so counter-based alerts are not sent for it.
    if not production.in_production(device):
        return False, ""

    threshold = threshold_for(rule, device)
    # Counter alerts use the counters shown on the dashboard, so a reset there
    # also clears the condition.
    if rule.condition == "scrap_rate":
        if state.shown_count > 0 and state.shown_scrap_rate >= threshold:
            pct = state.shown_scrap_rate * 100
            return True, (
                f"High scrap on '{device.name}' job '{state.job_name}': "
                f"{pct:.1f}% ({state.shown_fail}/{state.shown_count}) "
                f">= {threshold * 100:.1f}%"
            )
        return False, ""

    if rule.condition == "fail_count":
        if state.shown_fail >= threshold:
            return True, (
                f"Fail count on '{device.name}' job '{state.job_name}' "
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


def _rules(db: Session, conditions: list[str], device: Device | None) -> list[NotificationRule]:
    q = db.query(NotificationRule).filter(
        NotificationRule.enabled.is_(True), NotificationRule.condition.in_(conditions)
    )
    if device is not None:
        q = q.filter((NotificationRule.device_id == device.id) | (NotificationRule.device_id.is_(None)))
    return q.all()


def evaluate_device(db: Session, device: Device) -> None:
    """Check the state rules that apply to a device and fire the ones that match."""
    now = utcnow()
    check_production(db, device)
    active_state = (
        db.query(CounterState)
        .filter(CounterState.device_id == device.id, CounterState.is_active.is_(True))
        .first()
    )
    for rule in _rules(db, ["scrap_rate", "fail_count", "disconnected"], device):
        met, message = _condition_met(rule, device, active_state)
        if met and _cooldown_ok(rule, now):
            _fire(db, rule, message, now)


def emit(db: Session, condition: str, message: str, device: Device | None = None) -> None:
    """An event happened: send it through every enabled rule for it."""
    now = utcnow()
    for rule in _rules(db, [condition], device):
        if _cooldown_ok(rule, now):
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


def check_production(db: Session, device: Device) -> None:
    """Send production_change when the camera's production state changed
    since the last check. The first check only records the state."""
    current = production.state(device)
    previous = device.notified_state
    if previous == current:
        return
    device.notified_state = current
    db.commit()
    if previous is None:
        return
    text = _PRODUCTION_TEXT[current].format(timeout=device.idle_timeout_min or production.DEFAULT_IDLE_TIMEOUT_MIN)
    emit(db, "production_change", f"Device '{device.name}' {text}", device)


def check_all_production(db: Session) -> None:
    """Called periodically: a camera goes idle by time, with no reading."""
    for device in db.query(Device).filter(Device.enabled.is_(True)).all():
        check_production(db, device)


def startup_notice(version: str, previous: str | None, delay: float = STARTUP_NOTICE_DELAY) -> None:
    if previous and previous != version:
        message = f"Scrap Meter was updated from version {previous} to {version} and is running."
    else:
        message = f"Scrap Meter {version} started."
    timer = threading.Timer(delay, emit_system, args=("app_started", message))
    timer.daemon = True
    timer.start()
