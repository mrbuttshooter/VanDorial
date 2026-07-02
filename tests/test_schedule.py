"""
Campaign schedule windows: pure time math (gencall.core.schedule).
"""

from gencall.core import schedule as s


def test_daytime_window_half_open():
    start, end = 8 * 60, 20 * 60
    assert not s.in_window(7 * 60 + 59, start, end)
    assert s.in_window(8 * 60, start, end)          # start is inclusive
    assert s.in_window(19 * 60 + 59, start, end)
    assert not s.in_window(20 * 60, start, end)     # end is exclusive


def test_overnight_window_wraps_midnight():
    start, end = 22 * 60, 6 * 60
    assert s.in_window(23 * 60, start, end)
    assert s.in_window(0, start, end)
    assert s.in_window(5 * 60 + 59, start, end)
    assert not s.in_window(6 * 60, start, end)
    assert not s.in_window(12 * 60, start, end)


def test_start_equals_end_is_always_on():
    for minute in (0, 8 * 60, 23 * 60 + 59):
        assert s.in_window(minute, 9 * 60, 9 * 60)


def test_normalize_and_out_of_range_minutes():
    # 1440 (== 24:00) folds to 0; enforcement clamps rather than crashing.
    assert s.normalize_minute(1440) == 0
    assert s.normalize_minute(1500) == 60
    assert s.in_window(1440, 0, 0)


def test_local_minute_of_day_applies_tz_offset():
    # 12:00 UTC -> 15:00 at +3, 09:00 at -3, with day wrap.
    assert s.local_minute_of_day(12 * 3600, 3) == 15 * 60
    assert s.local_minute_of_day(12 * 3600, -3) == 9 * 60
    # 01:00 UTC at -3 wraps to 22:00 the previous local day.
    assert s.local_minute_of_day(1 * 3600, -3) == 22 * 60


def test_is_active_composed():
    # 12:00 UTC, window 08:00–20:00 local at +0 -> active.
    assert s.is_active(12 * 3600, 8 * 60, 20 * 60, 0)
    # Same instant at tz +11 is 23:00 local -> outside the daytime window.
    assert not s.is_active(12 * 3600, 8 * 60, 20 * 60, 11)
    # Overnight window active at 02:00 local.
    assert s.is_active(2 * 3600, 22 * 60, 6 * 60, 0)


def test_format_window():
    assert s.format_window(8 * 60, 20 * 60) == "08:00–20:00"
    assert s.format_window(22 * 60 + 30, 6 * 60 + 15) == "22:30–06:15"
