"""Database models.

Design notes on the counter logic (the heart of this app):

* Each Device has a live CounterState per job name. The camera exposes its own
  pass/fail counters which operators may reset at any time. We never want to
  lose counts across a reset, so every poll we compute the *delta* since the
  previous reading and add it to a running global total.
* A camera reset is detected when a raw counter drops below the value we saw
  last time. In that case the delta is the new raw value itself (the camera
  restarted from zero), not raw_now - raw_prev (which would be negative).
* When the job name changes, the running totals for the previous job are
  frozen (kept in the DB) and a fresh CounterState starts for the new job.
* A user can reset the counters shown on the dashboard. That only moves a
  baseline (base_*): the totals keep counting, so the readings history and
  the scrap statistics, which are built from the totals, do not change.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# --------------------------------------------------------------------------- #
# Users, roles and permissions
# --------------------------------------------------------------------------- #

# Granular permission keys. Admin implicitly has all of them.
PERMISSIONS = {
    "view_dashboard": "View dashboards and device data",
    "manage_devices": "Create, edit and delete devices",
    "control_connections": "Start/stop device connections, polling and production; reset counters",
    "view_data": "Browse logged readings and counters",
    "exclude_readings": "Exclude readings from the scrap statistics",
    "manage_notifications": "Configure notification rules and providers",
    "manage_users": "Create users and edit their permissions",
}


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # list[str] of permission keys; ignored when is_admin is True
    permissions: Mapped[list] = mapped_column(JSON, default=list)

    def has_permission(self, key: str) -> bool:
        if self.is_admin:
            return True
        return key in (self.permissions or [])


# --------------------------------------------------------------------------- #
# Devices (cameras)
# --------------------------------------------------------------------------- #


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=23)

    # one of the keys in app.protocols.registry.available()
    protocol: Mapped[str] = mapped_column(String(40))
    # protocol-specific settings (register addresses, telegram layout, ...)
    protocol_config: Mapped[dict] = mapped_column(JSON, default=dict)

    poll_interval: Mapped[int] = mapped_column(Integer, default=5)  # seconds
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # updated by the poller
    connected: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_poll_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    current_job: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Production state (see app.production). A camera is in production until
    # its pass counter has not increased for idle_timeout_min minutes; an
    # operator can also stop it by hand (manual_stop) until Start is pressed.
    idle_timeout_min: Mapped[int] = mapped_column(Integer, default=30)
    manual_stop: Mapped[bool] = mapped_column(Boolean, default=False)
    last_pass_change_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # production state last reported by a "production_change" notification
    notified_state: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    # Whether this device's readings count in the overall statistics (Scrap
    # statistics totals, the Excel export, chat command totals): "include" or
    # "exclude". Readings of an "exclude" device can still be included one at
    # a time or by period (Reading.included).
    stats_default: Mapped[str] = mapped_column(String(16), default="include")

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    counters: Mapped[list["CounterState"]] = relationship(
        back_populates="device", cascade="all, delete-orphan"
    )
    readings: Mapped[list["Reading"]] = relationship(
        back_populates="device", cascade="all, delete-orphan"
    )

    @property
    def excluded_by_default(self) -> bool:
        return self.stats_default == "exclude"

    @property
    def production_state(self) -> str:
        from . import production  # local import: production imports this module

        return production.state(self)


class CounterState(Base):
    """Running, reset-proof totals for a (device, job) pair."""

    __tablename__ = "counter_states"
    __table_args__ = (UniqueConstraint("device_id", "job_name", name="uq_device_job"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"), index=True)
    job_name: Mapped[str] = mapped_column(String(255), index=True)

    # accumulated totals that survive camera-side counter resets
    total_pass: Mapped[int] = mapped_column(Integer, default=0)
    total_fail: Mapped[int] = mapped_column(Integer, default=0)
    total_count: Mapped[int] = mapped_column(Integer, default=0)

    # last raw values read from the camera, used for delta/reset detection
    last_raw_pass: Mapped[int] = mapped_column(Integer, default=0)
    last_raw_fail: Mapped[int] = mapped_column(Integer, default=0)
    last_raw_count: Mapped[int] = mapped_column(Integer, default=0)

    # totals at the last reset from the dashboard; shown = total - base
    base_pass: Mapped[int] = mapped_column(Integer, default=0)
    base_fail: Mapped[int] = mapped_column(Integer, default=0)
    base_count: Mapped[int] = mapped_column(Integer, default=0)
    reset_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    device: Mapped["Device"] = relationship(back_populates="counters")

    @property
    def scrap_rate(self) -> float:
        if self.total_count <= 0:
            return 0.0
        return self.total_fail / self.total_count

    # Counters as shown on the dashboard: since the last reset (if any).
    @property
    def shown_pass(self) -> int:
        return max(self.total_pass - (self.base_pass or 0), 0)

    @property
    def shown_fail(self) -> int:
        return max(self.total_fail - (self.base_fail or 0), 0)

    @property
    def shown_count(self) -> int:
        return max(self.total_count - (self.base_count or 0), 0)

    @property
    def shown_scrap_rate(self) -> float:
        return self.shown_fail / self.shown_count if self.shown_count > 0 else 0.0

    def reset_shown(self, now: dt.datetime | None = None) -> None:
        """Start the dashboard counters from zero; the totals keep counting."""
        self.base_pass = self.total_pass
        self.base_fail = self.total_fail
        self.base_count = self.total_count
        self.reset_at = now or utcnow()


class Reading(Base):
    """A raw snapshot from one poll, kept for history/audit."""

    __tablename__ = "readings"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"), index=True)
    job_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    raw_pass: Mapped[int] = mapped_column(Integer, default=0)
    raw_fail: Mapped[int] = mapped_column(Integer, default=0)
    raw_count: Mapped[int] = mapped_column(Integer, default=0)

    # the running totals at the moment of this reading
    total_pass: Mapped[int] = mapped_column(Integer, default=0)
    total_fail: Mapped[int] = mapped_column(Integer, default=0)

    # everything else the protocol returned (jobname, custom tags, ...)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)

    # Scrap statistics: the parts counted since the previous reading are left
    # out when the reading is excluded by a user, or when the camera was not
    # in production (idle or stopped, see app.production) at that moment.
    # in_production is None for readings logged before it was recorded.
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    # included by a user although its device is excluded from the statistics
    # by default (Device.stats_default); ignored for "include" devices
    included: Mapped[bool] = mapped_column(Boolean, default=False)
    in_production: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    device: Mapped["Device"] = relationship(back_populates="readings")


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #


class NotificationRule(Base):
    __tablename__ = "notification_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    device_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("devices.id"), nullable=True
    )  # null = applies to all devices

    # a key of app.notifications.CONDITIONS, e.g. "scrap_rate", "disconnected"
    condition: Mapped[str] = mapped_column(String(40))
    # e.g. 0.05 for 5% scrap, or a fail-count threshold
    threshold: Mapped[float] = mapped_column(default=0.0)
    # rules for all cameras: {"<device id>": threshold} overrides per camera
    thresholds: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # "info" | "warning" | "alert": shown in front of the message
    severity: Mapped[str] = mapped_column(String(16), default="alert")
    # providers that receive this rule's messages; empty / null = all of them
    provider_ids: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # minimum seconds between two alerts for the same rule
    cooldown: Mapped[int] = mapped_column(Integer, default=300)
    last_fired_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NotificationProvider(Base):
    """Configured notifier, e.g. a WhatsApp group bridge."""

    __tablename__ = "notification_providers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(40))  # key in app.notifiers.registry
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NotificationLog(Base):
    __tablename__ = "notification_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    message: Mapped[str] = mapped_column(Text)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


# --------------------------------------------------------------------------- #
# Chat commands (WhatsApp group messages like "!status", see app.commands)
# --------------------------------------------------------------------------- #


class ChatCommand(Base):
    __tablename__ = "chat_commands"

    id: Mapped[int] = mapped_column(primary_key=True)
    # the word after the prefix, lower case, e.g. "status"
    keyword: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(200), default="")
    # which counts the reply shows: "dashboard" (counters on the dashboard),
    # "today" (production time today, like Scrap statistics) or "hours"
    period: Mapped[str] = mapped_column(String(16), default="dashboard")
    hours: Mapped[int] = mapped_column(Integer, default=8)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    # reply = header, one line per camera, footer; {placeholders} are filled in
    header: Mapped[str] = mapped_column(Text, default="")
    line: Mapped[str] = mapped_column(Text, default="")
    footer: Mapped[str] = mapped_column(Text, default="")
    # group ids ("…@g.us") where the command answers; empty = every group
    group_ids: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CommandLog(Base):
    __tablename__ = "command_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat: Mapped[str] = mapped_column(String(120))
    chat_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    sender: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    keyword: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    # "answered" | "not_allowed" | "unknown" | "failed" | "too_fast"
    outcome: Mapped[str] = mapped_column(String(16))
    reply: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
