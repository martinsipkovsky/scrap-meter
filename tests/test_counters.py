"""Tests for the reset-proof counter deltas."""
from app.counters import Sample, deltas


def test_deltas_normal_and_after_reset():
    assert deltas((10, 2, 12), (25, 5, 30)) == (15, 3, 18)
    assert deltas((5, 1, 6), (5, 1, 6)) == (0, 0, 0)
    # camera counters reset to a low value: the new values are all new pieces
    assert deltas((25, 5, 30), (4, 1, 5)) == (4, 1, 5)


def test_device_wide_reset_keeps_count_equal_to_pass_plus_fail():
    # Regression: on a reset an individual counter can land ABOVE its previous
    # value (fail 1 -> reset -> 2). The reset must be detected across the whole
    # read so fail is counted as +2, keeping total == pass + fail.
    assert deltas((19, 1, 20), (8, 2, 10)) == (8, 2, 10)


def test_values_a_source_does_not_read_add_nothing():
    # an OK-only source: NOK and total stay 0
    assert deltas((100, 0, 0), (5, 0, 0)) == (5, 0, 0)


def test_total_count_derived_from_pass_fail():
    s = Sample("J", raw_pass=7, raw_fail=3)
    assert s.raw_count == 10
