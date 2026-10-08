"""Database models.

Devices and stations:

* A Device is a connection: a camera, PLC, OPC UA server or counter the app
  reads (or that pushes to the app). Each read leaves the device's latest
  values in Device.last_values ({"pass": 120, "fail": 4, "job": "A"} for most
  protocols, node ids for OPC UA).
* A Station is what is counted. It has one or more sources (Station.sources):
  a source is one device plus which of its values give the OK, NOK, total and
  job, each configured on its own, so a station can combine several devices
  (two cameras, or OK from one device and NOK from another). Counters,
  readings, production state, statistics, alerts and chat commands are all
  per station; app.stations explains how the sources are counted.

Design notes on the counter logic (the heart of this app):

* Each Station has a CounterState per job name. The devices expose their own
  pass/fail counters which operators may reset at any time. We never want to
  lose counts across a reset, so on every read we compute each source's
  *delta* since the previous read (SourceState) and add it to the running
  total of the source's job, but only while the station is in production.
* A reset is detected when a raw counter drops below the value we saw last
  time. In that case the delta is the new raw value itself (the counter
  restarted from zero), not raw_now - raw_prev (which would be negative).
* The jobs the sources are running now have is_active set (several, when
  sources run different jobs); the others are frozen (kept in the DB).
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
    Date,
    DateTime,
    ForeignKey,
    Index,
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
    "manage_devices": "Create, edit and delete devices and stations; set job cycle times",
    "control_connections": "Start/stop device connections, polling and production; reset counters",
    "view_data": "Browse logged readings and counters",
    "exclude_readings": "Exclude readings from the scrap statistics",
    "manual_entry": "Enter data manually (OK / NOK entries per station)",
    "manage_notifications": "Configure notification rules and providers",
    "chat_room": "Read and write in the Chat room",
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
    """A counted unit, e.g. machine "M1" with two cameras.

    ``sources`` lists where its pieces come from, one entry per device::

        [{"id": "s1", "device_id": 1, "ok": "pass", "nok": "fail",
          "count": null,      # optional total; else OK + NOK
          "job": "job",       # optional; else the station's job
          "start_count": 2, "start_window_s": 60},   # the start rule
         {"id": "s2", "device_id": 2, "ok": null, "nok": "ns=2;s=M1.Rejects", ...}]

    Up to 1.7 it was a dict of roles ({"ok": {"device_id": 1, "key": "pass"},
    ...}); ``source_list`` reads both (app.stations.normalize_sources).
    """

    __tablename__ = "stations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    default_job: Mapped[str] = mapped_column(String(255), default="MAIN")
    current_job: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # last time a reading was recorded for the station
    last_reading_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Production state (see app.production). A station goes into production by
    # a source's start rule and stays in it until no source's OK counter has
    # increased for idle_timeout_min minutes; an operator can also stop it by
    # hand (manual_stop) until Start is pressed.
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

    # up to 1.6: ideal seconds per part for the OEE. Cycle times are per job
    # since 1.7 (Job.ideal_cycle_s); app.jobs.upgrade copied this value to the
    # jobs the station had run. Kept so a downgrade still finds it.
    ideal_cycle_s: Mapped[Optional[float]] = mapped_column(nullable=True)

    # since 1.13: device web pages (HMIs) shown on the station view, in order:
    # [{"name": "Camera 1", "url": "http://10.0.0.5/", "height": 600}, ...]
    # (height in pixels, null = the default). See app.hmi.
    hmi_windows: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # since 1.17: the station's alerts are muted (app.mute): by whom, since
    # when, and whether until the next job change ("!mute" in a chat) or until
    # someone turns them on again (the switch on the station view)
    alerts_muted: Mapped[bool] = mapped_column(Boolean, default=False)
    muted_by: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    muted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    muted_until_job_change: Mapped[bool] = mapped_column(Boolean, default=False)

    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    counters: Mapped[list["CounterState"]] = relationship(
        back_populates="station", cascade="all, delete-orphan"
    )
    readings: Mapped[list["Reading"]] = relationship(
        back_populates="station", cascade="all, delete-orphan"
    )
    source_states: Mapped[list["SourceState"]] = relationship(cascade="all, delete-orphan")

    @property
    def excluded_by_default(self) -> bool:
        return self.stats_default == "exclude"

    @property
    def production_state(self) -> str:
        from . import production  # local import: production imports this module

        return production.state(self)

    def source_list(self) -> list[dict]:
        from .stations import normalize_sources  # local import: stations imports this module

        return normalize_sources(self.sources)

    def device_ids(self) -> set[int]:
        return {s["device_id"] for s in self.source_list()}


class SourceState(Base):
    """What the app knows about one source of a station between reads: the
    raw counters it read last (to compute the next delta), its job, when its
    OK last rose while counted, and the pieces it made while the station was
    not in production (``pending``, for the start rule). See app.stations."""

    __tablename__ = "source_states"
    __table_args__ = (UniqueConstraint("station_id", "source_id", name="uq_station_source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"), index=True)
    source_id: Mapped[str] = mapped_column(String(16))
    # the device and values the raw counters were read from; when the source is
    # edited to read something else, the next read starts a new baseline
    signature: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    baselined: Mapped[bool] = mapped_column(Boolean, default=False)
    last_pass: Mapped[int] = mapped_column(Integer, default=0)
    last_fail: Mapped[int] = mapped_column(Integer, default=0)
    last_count: Mapped[int] = mapped_column(Integer, default=0)
    job: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # last time its OK rose while the station counted (the source was active)
    last_ok_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # pieces seen while not in production: [[unix time, ok, nok, total], ...]
    pending: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    # pictures of a piece not judged yet, with a piece rule (app.pieces):
    # {"ok": n, "nok": n, "since": unix time of its first picture, "id": piece id}
    open_piece: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Job(Base):
    """A job (product, recipe) the stations count, by name: the names in
    CounterState.job_name / Reading.job_name. Made when a station first counts
    a job, or added on the Jobs tab before production; holds the job's cycle
    time for the OEE performance factor and its piece rule."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    # ideal seconds per piece at full speed (None = not set): always
    # shot_s / pieces_per_shot, kept here so OEE, daily data and reports read
    # one value
    ideal_cycle_s: Mapped[Optional[float]] = mapped_column(nullable=True)
    # since 1.15: the cycle time as set, X seconds per shot (machine cycle)
    # making Y pieces (cavities); 1.14 values became X s per 1 piece
    shot_s: Mapped[Optional[float]] = mapped_column(nullable=True)
    pieces_per_shot: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=1)
    # how pictures make pieces (app.pieces.normalize); None = 1 picture, 1 piece
    piece_rule: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


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
    # one station's readings over a time range (statistics, OEE, charts)
    __table_args__ = (Index("ix_readings_station_created", "station_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[Optional[int]] = mapped_column(ForeignKey("stations.id"), index=True, nullable=True)
    # the device that was read (up to 1.4: the counted device)
    device_id: Mapped[Optional[int]] = mapped_column(Integer, index=True, nullable=True)
    job_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    raw_pass: Mapped[int] = mapped_column(Integer, default=0)
    raw_fail: Mapped[int] = mapped_column(Integer, default=0)
    raw_count: Mapped[int] = mapped_column(Integer, default=0)

    # the running totals of its job at the moment of this reading
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

    # Since 1.8: the source (Station.sources id) that was read, and the pieces
    # this read added to the station's counters (0 while not in production).
    # None on older readings: their pieces are the difference of the totals
    # of consecutive readings of the same job (app.scrap_stats.parts).
    source_id: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    ok_added: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    nok_added: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # entered by hand on the station view (see the docstring)
    manual: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    entered_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    station: Mapped["Station"] = relationship(back_populates="readings")


class StationComment(Base):
    """A comment written on a station, with a snapshot of the station at that
    moment (its name, job, and the OK / NOK / scrap shown on the dashboard).
    The snapshot never changes, and comments stay when their station is
    deleted, so reports (the powerbi_station_comments view, app.comments)
    keep their history."""

    __tablename__ = "station_comments"

    id: Mapped[int] = mapped_column(primary_key=True)
    # no foreign key: the comment outlives its station
    station_id: Mapped[Optional[int]] = mapped_column(Integer, index=True, nullable=True)
    station_name: Mapped[str] = mapped_column(String(120))
    job_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # the dashboard counters of the current job at that moment (since the last
    # reset, see CounterState.shown_*); scrap_rate is NOK / all, 0..1
    ok_count: Mapped[int] = mapped_column(Integer, default=0)
    nok_count: Mapped[int] = mapped_column(Integer, default=0)
    scrap_rate: Mapped[float] = mapped_column(default=0.0)
    text: Mapped[str] = mapped_column(Text)
    author: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


# --------------------------------------------------------------------------- #
# Daily data (app.daily): one row per station and day, and per station, job
# and day, filled in by the app. Reports read them through the powerbi_daily_*
# views. The rows keep the station name, so they outlive the station.
# --------------------------------------------------------------------------- #


class DailyStation(Base):
    __tablename__ = "daily_stations"
    __table_args__ = (UniqueConstraint("day", "station_id", name="uq_daily_station"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # calendar day in the time zone of the daily data (DAILY_TZ setting)
    day: Mapped[dt.date] = mapped_column(Date, index=True)
    station_id: Mapped[int] = mapped_column(Integer, index=True)
    station_name: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    # seconds of the day covered: the whole day, or up to now for today
    window_s: Mapped[int] = mapped_column(Integer, default=0)
    # time in production (as the OEE meter counts it), and the part of it on
    # jobs with an ideal cycle time
    production_s: Mapped[int] = mapped_column(Integer, default=0)
    timed_production_s: Mapped[int] = mapped_column(Integer, default=0)
    ideal_s: Mapped[float] = mapped_column(default=0.0)
    # counted parts (like Scrap statistics), manual entries included
    ok: Mapped[int] = mapped_column(Integer, default=0)
    nok: Mapped[int] = mapped_column(Integer, default=0)
    manual_ok: Mapped[int] = mapped_column(Integer, default=0)
    manual_nok: Mapped[int] = mapped_column(Integer, default=0)
    # parts left out: readings excluded by a user, made while not in production
    excluded_ok: Mapped[int] = mapped_column(Integer, default=0)
    excluded_nok: Mapped[int] = mapped_column(Integer, default=0)
    idle_ok: Mapped[int] = mapped_column(Integer, default=0)
    idle_nok: Mapped[int] = mapped_column(Integer, default=0)
    # the station is in the statistics' totals (Station.stats_default)
    in_totals: Mapped[bool] = mapped_column(Boolean, default=True)
    readings: Mapped[int] = mapped_column(Integer, default=0)
    comments: Mapped[int] = mapped_column(Integer, default=0)
    jobs: Mapped[str] = mapped_column(Text, default="")
    first_reading_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reading_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # the day was over when it was computed (today's row is refreshed)
    complete: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DailyJob(Base):
    __tablename__ = "daily_jobs"
    __table_args__ = (UniqueConstraint("day", "station_id", "job", name="uq_daily_job"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[dt.date] = mapped_column(Date, index=True)
    station_id: Mapped[int] = mapped_column(Integer, index=True)
    station_name: Mapped[str] = mapped_column(String(120))
    job: Mapped[str] = mapped_column(String(255))
    ok: Mapped[int] = mapped_column(Integer, default=0)
    nok: Mapped[int] = mapped_column(Integer, default=0)
    manual_ok: Mapped[int] = mapped_column(Integer, default=0)
    manual_nok: Mapped[int] = mapped_column(Integer, default=0)
    production_s: Mapped[int] = mapped_column(Integer, default=0)
    # the job's ideal cycle time when computed (None = not set) and the ideal
    # time of the parts made
    ideal_cycle_s: Mapped[Optional[float]] = mapped_column(nullable=True)
    ideal_s: Mapped[float] = mapped_column(default=0.0)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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
    # only stations in production at some point in the last N days; null = all
    active_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
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


class StationEvent(Base):
    """Something done to a station outside its readings, listed in the Data
    log: alerts muted ("mute") or turned on again ("unmute")."""

    __tablename__ = "station_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    # who did it (a web user, or the sender in a chat); None = the app
    by: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    # "web" | "chat" | "job change"
    source: Mapped[str] = mapped_column(String(16))
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


# --------------------------------------------------------------------------- #
# Chat room (one messenger chat shown on the Chat room tab, see app.chatroom)
# --------------------------------------------------------------------------- #


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    # "whatsapp" | "telegram" and the chat id ("…@g.us", or a Telegram chat id)
    kind: Mapped[str] = mapped_column(String(16))
    chat: Mapped[str] = mapped_column(String(120), index=True)
    # "in": written in the chat; "out": sent by the app (from the web, an alert
    # or a command reply)
    direction: Mapped[str] = mapped_column(String(8))
    # who wrote it: the web user, the messenger name of the sender, or None for
    # the app itself
    author: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    text: Mapped[str] = mapped_column(Text)
    # "received" | "sent" | "failed"
    status: Mapped[str] = mapped_column(String(16), default="received")
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # the messenger's id of the message (incoming ones are stored once)
    external_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


# --------------------------------------------------------------------------- #
# Raw data tab (app.rawdb)
# --------------------------------------------------------------------------- #


class DbAuditLog(Base):
    """A change made on the Raw data tab: who, when, which table and row, and
    the values before and after (only the changed columns of an update; the
    whole row of an insert or delete, so it can be undone)."""

    __tablename__ = "db_audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64))
    table_name: Mapped[str] = mapped_column(String(64), index=True)
    row_key: Mapped[str] = mapped_column(String(255))
    # "update" | "insert" | "delete" | "undo"
    action: Mapped[str] = mapped_column(String(16))
    old_values: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    new_values: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # an undo names the change it reversed; that change names its undo
    undo_of: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    undone_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
