"""Database models.

Devices and stations:

* A Device is a connection: a camera, PLC, OPC UA server or counter the app
  reads (or that pushes to the app). Each read leaves the device's latest
  values in Device.last_values ({"pass": 120, "fail": 4, "job": "A"} for most
  protocols, node ids for OPC UA).
* A Station is what is counted: its OK, NOK, optional total and optional job
  each come from one device and one value of it (Station.sources), so one
  station can combine several devices. Counters, readings, production state,
  statistics, alerts and chat commands are all per station.

Design notes on the counter logic (the heart of this app):

* Each Station has a live CounterState per job name. The devices expose their
  own pass/fail counters which operators may reset at any time. We never want
  to lose counts across a reset, so every reading we compute the *delta* since
  the previous one and add it to a running global total.
* A reset is detected when a raw counter drops below the value we saw last
  time. In that case the delta is the new raw value itself (the counter
  restarted from zero), not raw_now - raw_prev (which would be negative).
* When the job name changes, the running totals for the previous job are
  frozen (kept in the DB) and a fresh CounterState starts for the new job.
* A user can reset the counters shown on the dashboard. That only moves a
  baseline (base_*): the totals keep counting, so the readings history and
  the scrap statistics, which are built from the totals, do not change.

Up to version 1.4 a device was also the counted unit. Its counting fields are
still in the devices table, and app.stations.upgrade turns every such device
into a station with the same id (so readings, counters and alert rules keep
their numbers).
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
    "manage_devices": "Create, edit and delete devices and stations",
    "control_connections": "Start/stop device connections, polling and production; reset counters",
    "view_data": "Browse logged readings and counters",
    "exclude_readings": "Exclude readings from the scrap statistics",
    "manual_entry": "Enter data manually (OK / NOK entries per station)",
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
# Devices (connections) and stations (what is counted)
# --------------------------------------------------------------------------- #


class Meta(Base):
    """Small key/value facts about the data itself, e.g. which one-off
    upgrades have run on it. Part of backups, so restoring an old backup
    makes the upgrades run again on its data."""

    __tablename__ = "meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


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
    # the device's latest values, e.g. {"pass": 120, "fail": 4, "job": "A"}
    # (OPC UA: node id -> value), read by the stations that use it
    last_values: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    current_job: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Up to 1.4 the device was the counted unit; these fields moved to Station
    # and are only read by the upgrade (app.stations.upgrade).
    idle_timeout_min: Mapped[int] = mapped_column(Integer, default=30)
    manual_stop: Mapped[bool] = mapped_column(Boolean, default=False)
    last_pass_change_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notified_state: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    stats_default: Mapped[str] = mapped_column(String(16), default="include")

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Station(Base):
    """A counted unit, e.g. machine "M1": OK from one device, NOK from another.

    ``sources`` maps a role to a device value::

        {"ok":    {"device_id": 1, "key": "pass"},
         "nok":   {"device_id": 2, "key": "ns=2;s=M1.Rejects"},
         "count": null,                      # optional total; else OK + NOK
         "job":   {"device_id": 1, "key": "job"}}   # optional; else default_job
    """

    __tablename__ = "stations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    sources: Mapped[dict] = mapped_column(JSON, default=dict)
    default_job: Mapped[str] = mapped_column(String(255), default="MAIN")
    current_job: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # last time a reading was recorded for the station
    last_reading_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Production state (see app.production). A station is in production until
    # its OK counter has not increased for idle_timeout_min minutes; an
    # operator can also stop it by hand (manual_stop) until Start is pressed.
    idle_timeout_min: Mapped[int] = mapped_column(Integer, default=30)
    manual_stop: Mapped[bool] = mapped_column(Boolean, default=False)
    last_pass_change_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # production state last reported by a "production_change" notification
    notified_state: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    # Whether this station's readings count in the overall statistics (Scrap
    # statistics totals, the Excel export, chat command totals, the OEE meter):
    # "include" or "exclude". Readings of an "exclude" station can still be
    # included one at a time or by period (Reading.included).
    stats_default: Mapped[str] = mapped_column(String(16), default="include")

    # ideal seconds per part, for the OEE performance factor (None = unknown)
    ideal_cycle_s: Mapped[Optional[float]] = mapped_column(nullable=True)

    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    counters: Mapped[list["CounterState"]] = relationship(
        back_populates="station", cascade="all, delete-orphan"
    )
    readings: Mapped[list["Reading"]] = relationship(
        back_populates="station", cascade="all, delete-orphan"
    )

    @property
    def excluded_by_default(self) -> bool:
        return self.stats_default == "exclude"

    @property
    def production_state(self) -> str:
        from . import production  # local import: production imports this module

        return production.state(self)

    def source(self, role: str) -> dict | None:
        src = (self.sources or {}).get(role)
        return src if src and src.get("device_id") and src.get("key") not in (None, "") else None

    def device_ids(self) -> set[int]:
        return {int(s["device_id"]) for r in ("ok", "nok", "count", "job") if (s := self.source(r))}


class CounterState(Base):
    """Running, reset-proof totals for a (station, job) pair."""

    __tablename__ = "counter_states"
    __table_args__ = (UniqueConstraint("station_id", "job_name", name="uq_station_job"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[Optional[int]] = mapped_column(ForeignKey("stations.id"), index=True, nullable=True)
    # up to 1.4: the device the counters belonged to (no longer used)
    device_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    job_name: Mapped[str] = mapped_column(String(255), index=True)

    # accumulated totals that survive camera-side counter resets
    total_pass: Mapped[int] = mapped_column(Integer, default=0)
    total_fail: Mapped[int] = mapped_column(Integer, default=0)
    total_count: Mapped[int] = mapped_column(Integer, default=0)

    # last raw values read from the camera, used for delta/reset detection
    last_raw_pass: Mapped[int] = mapped_column(Integer, default=0)
    last_raw_fail: Mapped[int] = mapped_column(Integer, default=0)
    last_raw_count: Mapped[int] = mapped_column(Integer, default=0)

    # parts added by manual entries (Reading.manual) for this job; kept apart
    # from total_* so the reading history's totals stay the devices' counts
    manual_pass: Mapped[int] = mapped_column(Integer, default=0)
    manual_fail: Mapped[int] = mapped_column(Integer, default=0)

    # totals at the last reset from the dashboard; shown = all - base
    base_pass: Mapped[int] = mapped_column(Integer, default=0)
    base_fail: Mapped[int] = mapped_column(Integer, default=0)
    base_count: Mapped[int] = mapped_column(Integer, default=0)
    reset_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    station: Mapped["Station"] = relationship(back_populates="counters")

    # Device counts plus manual entries
    @property
    def all_pass(self) -> int:
        return self.total_pass + (self.manual_pass or 0)

    @property
    def all_fail(self) -> int:
        return self.total_fail + (self.manual_fail or 0)

    @property
    def all_count(self) -> int:
        return self.total_count + (self.manual_pass or 0) + (self.manual_fail or 0)

    @property
    def scrap_rate(self) -> float:
        if self.all_count <= 0:
            return 0.0
        return self.all_fail / self.all_count

    # Counters as shown on the dashboard: since the last reset (if any).
    @property
    def shown_pass(self) -> int:
        return max(self.all_pass - (self.base_pass or 0), 0)

    @property
    def shown_fail(self) -> int:
        return max(self.all_fail - (self.base_fail or 0), 0)

    @property
    def shown_count(self) -> int:
        return max(self.all_count - (self.base_count or 0), 0)

    @property
    def shown_scrap_rate(self) -> float:
        return self.shown_fail / self.shown_count if self.shown_count > 0 else 0.0

    def reset_shown(self, now: dt.datetime | None = None) -> None:
        """Start the dashboard counters from zero; the totals keep counting."""
        self.base_pass = self.all_pass
        self.base_fail = self.all_fail
        self.base_count = self.all_count
        self.reset_at = now or utcnow()


class Reading(Base):
    """A station's values at one moment, kept for history/audit.

    A manual entry (manual=True) is not a snapshot: raw_pass / raw_fail are
    the parts entered by hand, total_* stay 0, and statistics count it apart
    from the device readings' totals (see app.scrap_stats.station_parts).
    """

    __tablename__ = "readings"

    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[Optional[int]] = mapped_column(ForeignKey("stations.id"), index=True, nullable=True)
    # the device that supplied the OK value (up to 1.4: the counted device)
    device_id: Mapped[Optional[int]] = mapped_column(Integer, index=True, nullable=True)
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
    # out when the reading is excluded by a user, or when the station was not
    # in production (idle or stopped, see app.production) at that moment.
    # in_production is None for readings logged before it was recorded.
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    # included by a user although its station is excluded from the statistics
    # by default (Station.stats_default); ignored for "include" stations
    included: Mapped[bool] = mapped_column(Boolean, default=False)
    in_production: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    # entered by hand on the station view (see the docstring)
    manual: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    entered_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    station: Mapped["Station"] = relationship(back_populates="readings")


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #


class NotificationRule(Base):
    __tablename__ = "notification_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    station_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("stations.id"), nullable=True
    )  # null = applies to all stations
    # up to 1.4 the rule's device; the upgrade copies it to station_id
    device_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # a key of app.notifications.CONDITIONS, e.g. "scrap_rate", "disconnected"
    condition: Mapped[str] = mapped_column(String(40))
    # e.g. 0.05 for 5% scrap, or a fail-count threshold
    threshold: Mapped[float] = mapped_column(default=0.0)
    # rules for all stations: {"<station id>": threshold} overrides per station
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
    # reply = header, one line per station, footer; {placeholders} are filled in
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
