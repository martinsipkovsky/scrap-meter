"""Tests for the reset-proof counter accumulation logic."""
from app.counters import Sample, apply_sample, compute_delta, new_state_for
from app.models import CounterState, Station


def _state():
    return CounterState(
        station_id=1, job_name="J", total_pass=0, total_fail=0, total_count=0,
        last_raw_pass=0, last_raw_fail=0, last_raw_count=0, is_active=True,
    )


def test_compute_delta_normal():
    assert compute_delta(10, 4) == 6


def test_compute_delta_reset_to_zero():
    # camera reset: previous 100, now 3 -> the 3 new parts count, nothing lost
    assert compute_delta(3, 100) == 3


def test_compute_delta_same():
    assert compute_delta(5, 5) == 0


def test_accumulate_across_reset():
    st = _state()
    apply_sample(st, Sample("J", raw_pass=10, raw_fail=2))
    assert (st.total_pass, st.total_fail) == (10, 2)
    # more parts, no reset: deltas are 15 pass / 3 fail
    apply_sample(st, Sample("J", raw_pass=25, raw_fail=5))
    assert (st.total_pass, st.total_fail) == (25, 5)
    # camera counters reset to a low value; totals must keep growing
    apply_sample(st, Sample("J", raw_pass=4, raw_fail=1))
    assert st.total_pass == 25 + 4
    assert st.total_fail == 5 + 1


def test_total_count_derived_from_pass_fail():
    s = Sample("J", raw_pass=7, raw_fail=3)
    assert s.raw_count == 10


def test_new_state_baselines_first_sample():
    dev = Station(id=1)
    st = new_state_for(dev, Sample("J", raw_pass=50, raw_fail=5))
    # first poll should not double count an already-running camera
    assert st.total_pass == 50
    assert st.last_raw_pass == 50


def test_device_wide_reset_keeps_count_equal_to_pass_plus_fail():
    # Regression: on a reset an individual counter can land ABOVE its previous
    # value (fail 1 -> reset -> 2). The reset must be detected across the whole
    # sample so fail is counted as +2, keeping total_count == pass + fail.
    st = _state()
    apply_sample(st, Sample("J", raw_pass=19, raw_fail=1))   # count 20
    assert (st.total_pass, st.total_fail, st.total_count) == (19, 1, 20)
    # camera resets: raw drops overall, but fail (2) > previous fail (1)
    apply_sample(st, Sample("J", raw_pass=8, raw_fail=2))    # count 10
    assert st.total_count == st.total_pass + st.total_fail
    assert (st.total_pass, st.total_fail) == (27, 3)


def test_scrap_rate():
    st = _state()
    apply_sample(st, Sample("J", raw_pass=90, raw_fail=10))
    assert abs(st.scrap_rate - 0.1) < 1e-9


def test_unlinked_counters_reset_on_their_own():
    """OK and NOK from different devices: a reset of one is not a reset of the other."""
    s = _state()
    apply_sample(s, Sample("J", raw_pass=100, raw_fail=10), linked=False, own_count=False)
    assert (s.total_pass, s.total_fail, s.total_count) == (100, 10, 110)
    # the OK device was reset, the NOK device kept counting
    apply_sample(s, Sample("J", raw_pass=5, raw_fail=12), linked=False, own_count=False)
    assert (s.total_pass, s.total_fail, s.total_count) == (105, 12, 117)
