"""Protocol driver contract.

Every device protocol lives in its own file in this package and subclasses
``ProtocolDriver``. To troubleshoot one protocol you open exactly one file.

A driver's only job is: connect to the device, read one sample, normalise it
into a ``counters.Sample`` (or a dict of values, ``read_values``), and
disconnect. Accumulation, persistence and
scheduling are handled elsewhere (app.counters / app.poller).
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..counters import Sample, sample_values


class ProtocolError(Exception):
    """Raised by a driver when it cannot talk to the device."""


class ProtocolDriver(ABC):
    #: unique key stored on Device.protocol
    key: str = ""
    #: human label shown in the UI
    label: str = ""
    #: description of the ``config`` dict this driver accepts, for the UI form
    config_fields: dict[str, str] = {}

    def __init__(self, host: str, port: int, config: dict | None = None):
        self.host = host
        self.port = port
        self.config = config or {}

    @abstractmethod
    def read(self) -> Sample:
        """Open a connection, read one sample, return it. Raise ProtocolError."""
        raise NotImplementedError

    def read_values(self, keys: set[str]) -> dict:
        """The device's values for stations: {"pass", "fail", "count", "job"}.

        ``keys`` are the values the device's stations use; drivers that can
        read any value by name (OPC UA) read just those. A key that could not
        be read may be listed in "_errors" as {key: message}.
        """
        return sample_values(self.read())

    # Optional lifecycle hooks for drivers that keep a persistent socket.
    def open(self) -> None:  # pragma: no cover - default no-op
        pass

    def close(self) -> None:  # pragma: no cover - default no-op
        pass
