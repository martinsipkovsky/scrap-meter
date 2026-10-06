"""Protocol driver registry.

Each protocol is one file in this package. Register new drivers here by adding
them to ``_DRIVERS``; the rest of the app discovers them through this registry
so a protocol can be added or troubleshooted in isolation.
"""
from __future__ import annotations

from .base import ProtocolDriver, ProtocolError
from .datachannel import DataChannelDriver
from .modbus import ModbusDriver
from .opcua import OpcUaDriver
from .profinet import ProfinetDriver
from .simulator import SimulatorDriver
from .slmp import SlmpDriver
from .slmp_server import SlmpServerDriver
from .tcp import TcpDriver
from .tcp_listener import TcpListenerDriver
from .udp_listener import UdpListenerDriver

_DRIVERS: dict[str, type[ProtocolDriver]] = {
    d.key: d
    for d in (
        DataChannelDriver,
        ModbusDriver,
        OpcUaDriver,
        TcpDriver,
        TcpListenerDriver,
        UdpListenerDriver,
        SlmpDriver,
        SlmpServerDriver,
        ProfinetDriver,
        SimulatorDriver,
    )
}


def available() -> dict[str, type[ProtocolDriver]]:
    return dict(_DRIVERS)


def describe() -> list[dict]:
    """UI-friendly metadata for every protocol."""
    return [
        {"key": d.key, "label": d.label, "config_fields": d.config_fields,
         "push": is_push(d.key), "push_help": getattr(d, "push_help", "")}
        for d in _DRIVERS.values()
    ]


def is_push(protocol: str) -> bool:
    """True for protocols where the camera connects to the app (not polled)."""
    cls = _DRIVERS.get(protocol)
    return bool(cls and getattr(cls, "push", False))


def push_keys() -> list[str]:
    return [k for k in _DRIVERS if is_push(k)]


def get_driver(protocol: str, host: str, port: int, config: dict | None = None) -> ProtocolDriver:
    cls = _DRIVERS.get(protocol)
    if cls is None:
        raise ProtocolError(f"Unknown protocol: {protocol}")
    return cls(host=host, port=port, config=config or {})


__all__ = ["available", "describe", "get_driver", "is_push", "push_keys", "ProtocolDriver", "ProtocolError"]
