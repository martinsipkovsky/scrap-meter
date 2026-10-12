"""Reset-proof counter deltas.

This module is deliberately pure/self-contained so it is easy to test and to
troubleshoot: given the raw counters a station source read last time and the
ones it reads now, it computes how many pieces were made in between.

Rules (see also models.py docstring):

* delta = raw_now - raw_prev, normally.
* A counter that dropped below what we last saw restarted from zero: its
  delta is its new raw value (everything counted since the reset). Any counts
  between the last read and the reset are unavoidably not observable, but we
  never subtract and never lose banked totals.
* Counters are judged one by one, because devices reset them separately
  (an OK counter cleared per box while the NOK counter runs all day): a
  counter that did not drop keeps counting from its old value. Up to 1.25 a
  drop of any counter restarted all of them, which counted the whole NOK
  counter again every time only the OK counter was reset.
* One case cannot be told apart from two reads alone: after a reset of the
  whole device a counter can land above its old value (fail 1 -> reset -> 2),
  which looks like +1. Such a counter is taken as restarted with the others
  only when that is the likelier story: it rose, and its old value was no
  more than the pieces the restarted counters have made since (1 <= 8 in
  (19, 1) -> (8, 2), so +2; 150 > 1 in (100, 150) -> (1, 151), so +1).
* A total that is the sum of OK and NOK before and after follows them.
* A device that says it restarted its own tally (an event-mode listener,
  "_restarted") is simply counted from zero (app.stations).
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
    station's first read of it is all new pieces, not a baseline; when it
    started that tally again (a new job, a restart) the record says so
    ("_restarted") and is counted from zero too."""
    values = {"pass": sample.raw_pass, "fail": sample.raw_fail, "count": sample.raw_count, "job": sample.job_name}
    extra = sample.extra or {}
    if extra.get("mode") == "event":
        values["_from_zero"] = True
        if extra.get("restarted"):
            values["_restarted"] = True
    return values


def deltas(prev: tuple[int, int, int], now: tuple[int, int, int]) -> tuple[int, int, int]:
    """(pass, fail, count) made between two reads of one device's counters.

    Each counter on its own: one that dropped restarted, so its new raw value
    is all new pieces; the others add their rise (see the module doc for the
    one exception and for the total). A counter the source doesn't read is 0
    in both and adds nothing.
    """
    dropped = [n < p for n, p in zip(now, prev)]
    if not any(dropped):
        return tuple(n - p for n, p in zip(now, prev))  # type: ignore[return-value]
    # what the restarted counters show since the reset (the total only when
    # it is the one that dropped: as a sum it drops with either of the others)
    made = sum(max(now[i], 0) for i in (0, 1) if dropped[i]) or max(now[2], 0)
    out = []
    for n, p, fell in zip(now, prev, dropped):
        restarted = fell or (0 < p <= made and n > p)
        out.append(max(n, 0) if restarted else n - p)
    if prev[2] == prev[0] + prev[1] and now[2] == now[0] + now[1]:  # a total that is their sum
        out[2] = out[0] + out[1]
    return tuple(out)  # type: ignore[return-value]


def deltas_1_25(prev: tuple[int, int, int], now: tuple[int, int, int]) -> tuple[int, int, int]:
    """``deltas`` as it was up to 1.25 (a drop of any counter restarted them
    all). Only app.recount uses it, to find the readings that rule inflated."""
    if any(n < p for n, p in zip(now, prev)):
        return tuple(max(n, 0) for n in now)  # type: ignore[return-value]
    return tuple(n - p for n, p in zip(now, prev))  # type: ignore[return-value]
