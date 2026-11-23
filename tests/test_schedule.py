"""Off-peak windows: a helper whose endpoint carries hours only works inside them."""

import time

import pytest

from decompnet.app import schedule


def at(hh, mm=0):
    """Seconds since the epoch for a UTC wall clock today, built without depending on the local zone."""
    return time.mktime(time.gmtime()) - (time.gmtime().tm_hour * 3600 + time.gmtime().tm_min * 60 +
                                         time.gmtime().tm_sec) + hh * 3600 + mm * 60


def test_no_schedule_is_always_open():
    assert schedule.open_at(None)
    assert schedule.open_at("")
    assert schedule.next_start(None) is None
    assert schedule.describe(None) == "any time"


def test_a_plain_window():
    assert schedule.open_at("09:00-17:00", at(10))
    assert not schedule.open_at("09:00-17:00", at(8, 59))
    assert not schedule.open_at("09:00-17:00", at(17))  # the end is exclusive
    assert schedule.open_at("09:00-17:00", at(16, 59))


def test_a_window_that_wraps_midnight_is_deepseek_s_off_peak():
    """16:30-00:30 UTC: open in the evening and just past midnight, shut in the afternoon."""
    assert schedule.open_at("16:30-00:30", at(18))
    assert schedule.open_at("16:30-00:30", at(0, 15))
    assert not schedule.open_at("16:30-00:30", at(9))
    assert not schedule.open_at("16:30-00:30", at(16, 29))


def test_next_start_says_how_long_to_wait():
    when = at(9)  # before the evening window
    starts = schedule.next_start("16:30-00:30", when)
    assert starts is not None
    assert 7.5 * 3600 - 61 <= starts - when <= 7.5 * 3600 + 61  # 09:00 to 16:30
    assert schedule.next_start("16:30-00:30", at(20)) is None  # already inside


def test_several_windows_pick_the_nearest_opening():
    hours = "00:30-08:00,16:30-24:00"
    assert schedule.open_at(hours, at(2))
    assert schedule.open_at(hours, at(17))
    assert not schedule.open_at(hours, at(12))
    start = schedule.next_start(hours, at(12))
    assert start is not None and start - at(12) <= 4.5 * 3600 + 61  # 12:00 -> 16:30


def test_unreadable_schedules_are_refused():
    for bad in ("09:00", "9-17", "09:00-09:00", "25:00-26:00", "09:00-17:00;12:00-13:00"):
        with pytest.raises(ValueError):
            schedule.parse(bad)


def test_describe_reads_back_like_it_was_written():
    assert schedule.describe("16:30-00:30") == "16:30-00:30 UTC"
    assert schedule.describe("0:30-8:00,16:30-24:00") == "00:30-08:00, 16:30-24:00 UTC"
