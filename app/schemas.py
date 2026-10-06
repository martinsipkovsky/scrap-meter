"""Pydantic request/response models for the JSON API."""
from __future__ import annotations

import datetime as dt
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---- Users ----------------------------------------------------------------
class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1)
    is_admin: bool = False
    permissions: list[str] = []


class UserUpdate(BaseModel):
    password: Optional[str] = None
    is_admin: Optional[bool] = None
    is_active: Optional[bool] = None
    permissions: Optional[list[str]] = None


class UserOut(BaseModel):
    id: int
    username: str
    is_admin: bool
    is_active: bool
    permissions: list[str]

    model_config = ConfigDict(from_attributes=True)


# ---- Devices --------------------------------------------------------------
class DeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    host: str
    port: int = 23
    protocol: str
    protocol_config: dict = {}
    poll_interval: int = 5
    enabled: bool = True
    # also make a station counting this device's pass / fail / total / job
    # (OPC UA: only when the config names pass or fail nodes)
    create_station: bool = False


class DeviceUpdate(BaseModel):
    name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    protocol: Optional[str] = None
    protocol_config: Optional[dict] = None
    poll_interval: Optional[int] = None
    enabled: Optional[bool] = None


class DeviceOut(BaseModel):
    id: int
    name: str
    host: str
    port: int
    protocol: str
    protocol_config: dict
    poll_interval: int
    enabled: bool
    connected: bool
    last_error: Optional[str]
    last_poll_at: Optional[dt.datetime]
    current_job: Optional[str]
    last_values: Optional[dict]

    model_config = ConfigDict(from_attributes=True)


class DeviceExportItem(BaseModel):
    """One device in an export file: configuration only, no counters/history.

    Files from 1.4 and older also carry the station settings of the device
    (idle_timeout_min, stats_default); importing such a file makes a station
    for each new device."""

    name: str = Field(min_length=1, max_length=120)
    host: str = ""
    port: int = 23
    protocol: str
    protocol_config: dict = {}
    poll_interval: int = 5
    enabled: bool = True
    idle_timeout_min: int = Field(default=30, ge=1, le=10080)
    stats_default: Literal["include", "exclude"] = "include"


# ---- Stations -------------------------------------------------------------
class StationSource(BaseModel):
    device_id: int
    key: str = Field(min_length=1, max_length=500)


class StationSources(BaseModel):
    ok: Optional[StationSource] = None
    nok: Optional[StationSource] = None
    count: Optional[StationSource] = None
    job: Optional[StationSource] = None


class StationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    sources: StationSources
    default_job: str = Field(default="MAIN", min_length=1, max_length=255)
    # minutes without an OK increase before the station counts as not in production
    idle_timeout_min: int = Field(default=30, ge=1, le=10080)
    # whether the station's readings count in the overall statistics
    stats_default: Literal["include", "exclude"] = "include"
    # ideal seconds per part (OEE performance); None = unknown
    ideal_cycle_s: Optional[float] = Field(default=None, gt=0, le=86400)
    sort_order: int = 0


class StationUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    sources: Optional[StationSources] = None
    default_job: Optional[str] = Field(default=None, min_length=1, max_length=255)
    idle_timeout_min: Optional[int] = Field(default=None, ge=1, le=10080)
    stats_default: Optional[Literal["include", "exclude"]] = None
    ideal_cycle_s: Optional[float] = Field(default=None, gt=0, le=86400)
    sort_order: Optional[int] = None


class StationExportSource(BaseModel):
    device: str  # device name
    key: str


class StationExportItem(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    sources: dict[str, Optional[StationExportSource]] = {}
    default_job: str = "MAIN"
    idle_timeout_min: int = Field(default=30, ge=1, le=10080)
    stats_default: Literal["include", "exclude"] = "include"
    ideal_cycle_s: Optional[float] = Field(default=None, gt=0, le=86400)
    sort_order: int = 0


class DeviceImport(BaseModel):
    """An export file. "cameras" holds the devices (the name of the list in
    every version); "stations" is there from version 2 (1.5)."""

    version: int = 1
    cameras: list[DeviceExportItem]
    stations: Optional[list[StationExportItem]] = None


# ---- Notifications --------------------------------------------------------
class RuleCreate(BaseModel):
    name: str
    station_id: Optional[int] = None  # None = all stations
    condition: str
    threshold: float = 0.0
    # per-station thresholds for a rule on all stations: {"<station id>": value}
    thresholds: Optional[dict[str, Optional[float]]] = None
    severity: str = "alert"
    # providers that receive it; empty = all enabled providers
    provider_ids: list[int] = []
    enabled: bool = True
    cooldown: int = Field(default=300, ge=0)


class RuleUpdate(BaseModel):
    name: Optional[str] = None
    station_id: Optional[int] = None
    condition: Optional[str] = None
    threshold: Optional[float] = None
    thresholds: Optional[dict[str, Optional[float]]] = None
    severity: Optional[str] = None
    provider_ids: Optional[list[int]] = None
    enabled: Optional[bool] = None
    cooldown: Optional[int] = Field(default=None, ge=0)


class ProviderCreate(BaseModel):
    name: str
    kind: str
    config: dict = {}
    enabled: bool = True


class ProviderUpdate(BaseModel):
    name: Optional[str] = None
    config: Optional[dict] = None
    enabled: Optional[bool] = None
