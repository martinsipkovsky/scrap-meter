"""Tests for the reset-proof counter deltas."""
from app.counters import Sample, deltas


def test_deltas_normal_and_after_reset():
    assert deltas((10, 2, 12), (25, 5, 30)) == (15, 3, 18)
    assert deltas((5, 1, 6), (5, 1, 6)) == (0, 0, 0)
    # camera counters reset to a low value: the new values are all new pieces
    assert deltas((25, 5, 30), (4, 1, 5)) == (4, 1, 5)


def test_a_whole_device_reset_keeps_count_equal_to_pass_plus_fail():
    # Regression: on a reset an individual counter can land ABOVE its previous
    # value (fail 1 -> reset -> 2). It restarted with the others (its old
    # value is no more than the 8 pieces OK has made since), so fail is +2,
    # keeping total == pass + fail.
    assert deltas((19, 1, 20), (8, 2, 10)) == (8, 2, 10)
    # also when the total lands exactly on its old value
    assert deltas((9, 1, 10), (8, 2, 10)) == (8, 2, 10)


def test_a_counter_that_did_not_reset_is_not_counted_again():
    # Regression (1.26): the OK counter is reset on its own, the NOK counter
    # runs on. Up to 1.25 the whole NOK counter was counted again.
    assert deltas((100, 150, 250), (0, 150, 150)) == (0, 0, 0)
    assert deltas((100, 150, 250), (1, 151, 152)) == (1, 1, 2)
    assert deltas((100, 150, 0), (3, 152, 0)) == (3, 2, 0)        # a source without a total
    # also when the NOK counter is still low and did not move
    assert deltas((40, 1, 41), (2, 1, 3)) == (2, 0, 2)
    # the other way round: only the NOK counter is reset
    assert deltas((500, 30, 530), (504, 1, 505)) == (4, 1, 5)
    # a total of its own that runs on while OK restarts
    assert deltas((100, 150, 300), (2, 151, 304)) == (2, 1, 4)
    # only the total restarts
    assert deltas((0, 150, 400), (0, 151, 3)) == (0, 1, 3)


def test_values_a_source_does_not_read_add_nothing():
    # an OK-only source: NOK and total stay 0
    assert deltas((100, 0, 0), (5, 0, 0)) == (5, 0, 0)


def test_total_count_derived_from_pass_fail():
    s = Sample("J", raw_pass=7, raw_fail=3)
    assert s.raw_count == 10
