"""Reset-proof counter accumulation.

This module is deliberately pure/self-contained so it is easy to test and to
troubleshoot: given the previous CounterState and a fresh raw sample from a
station, it computes the new running totals.

Rules (see also models.py docstring):

* delta = raw_now - raw_prev, normally.
* A counter reset on a device is a *device-wide* event: pass, fail and total
  all restart together. So reset detection is done at the sample level — if ANY of the
  raw counters dropped below what we last saw, we treat the whole sample as a
  reset and each counter's delta becomes its new raw value (everything counted
  since the reset). This matters because after a reset an individual counter
  can land on a value higher than its pre-reset value (e.g. fail 1 -> reset ->
  2); detecting the reset per-counter would misread that as a +1 increment and
  undercount. Any counts between the last poll and the reset are unavoidably
  not observable, but we never subtract and never lose banked totals.
* A station whose counters come from different devices (OK from one, NOK
  from another) is not reset together, so there each counter is checked on
  its own (``apply_sample(..., linked=False)``).
* When the job name changes, the caller starts a new CounterState; the old
  one is frozen with is_active = False.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import CounterState, Station


@dataclass
class Sample:
    """Normalised reading: returned by a protocol driver, and built for a
    station from its devices' values (app.stations)."""

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
    """A device's values as stations see them (see Station.sources)."""
    return {"pass": sample.raw_pass, "fail": sample.raw_fail, "count": sample.raw_count, "job": sample.job_name}


def compute_delta(raw_now: int, raw_prev: int) -> int:
    """Non-negative increment for a single counter, treating a drop as a reset.

    Used directly only when a counter is tracked on its own. For a device's
    pass/fail/count triple prefer ``apply_sample``, which detects the reset
    across the whole sample (see module docstring).
    """
    if raw_now >= raw_prev:
        return raw_now - raw_prev
    # counter was reset on the device; everything now is new
    return max(raw_now, 0)


def is_reset(sample: Sample, state: CounterState) -> bool:
    """A device-wide reset: any monotonic counter dropped since last poll."""
    return (
        sample.raw_pass < state.last_raw_pass
        or sample.raw_fail < state.last_raw_fail
        or sample.raw_count < state.last_raw_count
    )


def apply_sample(state: CounterState, sample: Sample, linked: bool = True,
                 own_count: bool = True) -> CounterState:
    """Fold one raw sample into a running CounterState (in place).

    ``linked``: the counters come from one device, so a drop in any of them
    means all were reset. False checks each counter on its own; then
    ``own_count`` False (no total counter, raw_count is pass + fail) makes the
    total grow by exactly the pass and fail increases.
    """
    if not linked:
        d_pass = compute_delta(sample.raw_pass, state.last_raw_pass)
        d_fail = compute_delta(sample.raw_fail, state.last_raw_fail)
        d_count = (compute_delta(sample.raw_count, state.last_raw_count) if own_count
                   else d_pass + d_fail)
    elif is_reset(sample, state):
        # every counter restarted; its new raw value is all-new counts
        d_pass = max(sample.raw_pass, 0)
        d_fail = max(sample.raw_fail, 0)
        d_count = max(sample.raw_count, 0)
    else:
        d_pass = sample.raw_pass - state.last_raw_pass
        d_fail = sample.raw_fail - state.last_raw_fail
        d_count = sample.raw_count - state.last_raw_count

    state.total_pass += d_pass
    state.total_fail += d_fail
    state.total_count += d_count

    state.last_raw_pass = sample.raw_pass
    state.last_raw_fail = sample.raw_fail
    state.last_raw_count = sample.raw_count
    return state


def new_state_for(station: Station, sample: Sample) -> CounterState:
    """Create a fresh CounterState seeded from the first sample of a job.

    The first sample establishes the baseline: its raw values become the
    starting totals so that a device that has already counted N parts for a
    job does not double-count on the first poll after the app starts watching.
    """
    return CounterState(
        station_id=station.id,
        job_name=sample.job_name,
        total_pass=sample.raw_pass,
        total_fail=sample.raw_fail,
        total_count=sample.raw_count,
        last_raw_pass=sample.raw_pass,
        last_raw_fail=sample.raw_fail,
        last_raw_count=sample.raw_count,
        is_active=True,
    )
