"""Reset-proof counter deltas.

This module is deliberately pure/self-contained so it is easy to test and to
troubleshoot: given the raw counters a station source read last time and the
ones it reads now, it computes how many pieces were made in between.

Rules (see also models.py docstring):

* delta = raw_now - raw_prev, normally.
* A counter reset on a device is a *device-wide* event: pass, fail and total
  all restart together. A source reads one device, so reset detection is
  done across the source's counters: if ANY of them dropped below what we
  last saw, the whole read is a reset and each counter's delta becomes its
  new raw value (everything counted since the reset). This matters because
  after a reset an individual counter can land on a value higher than its
  pre-reset value (e.g. fail 1 -> reset -> 2); detecting the reset
  per-counter would misread that as a +1 increment and undercount. Any counts
  between the last read and the reset are unavoidably not observable, but we
  never subtract and never lose banked totals.
* A station whose OK and NOK come from different devices has a source per
  device, so a reset of one device is not a reset of the other.
* Whether a delta is added to the station's counters (only while it is in
  production) is decided by app.stations.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Sample:
    """Normalised reading returned by a protocol driver."""

    job_name: str
    raw_pass: int = 0
    raw_fail: int = 0
    raw_count: int = 0
    extra: dict | None = None

    def __post_init__(self) -> None:
        if self.extra is None:
            self.extra = {}
        # If the device only reports pass+fail, derive the total.
        if self.raw_count == 0 and (self.raw_pass or self.raw_fail):
            self.raw_count = self.raw_pass + self.raw_fail


# the values every Sample offers to stations, with their labels
SAMPLE_KEYS = {"pass": "OK counter (pass)", "fail": "NOK counter (fail)",
               "count": "Total counter", "job": "Job name"}


def sample_values(sample: Sample) -> dict:
    """A device's values as stations see them (see Station.sources). An
    event-mode listener counts its own tally from zero ("_from_zero"), so a
    station's first read of it is all new pieces, not a baseline."""
    values = {"pass": sample.raw_pass, "fail": sample.raw_fail, "count": sample.raw_count, "job": sample.job_name}
    if (sample.extra or {}).get("mode") == "event":
        values["_from_zero"] = True
    return values


def deltas(prev: tuple[int, int, int], now: tuple[int, int, int]) -> tuple[int, int, int]:
    """(pass, fail, count) made between two reads of one device's counters.

    A drop in any of them means the device reset all of them, so each new
    raw value is all new pieces. A counter the source doesn't read is 0 in
    both and adds nothing.
    """
    if any(n < p for n, p in zip(now, prev)):
        return tuple(max(n, 0) for n in now)  # type: ignore[return-value]
    return tuple(n - p for n, p in zip(now, prev))  # type: ignore[return-value]
