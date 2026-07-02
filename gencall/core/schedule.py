"""
Campaign schedule windows (pure time math).

A campaign may carry an optional daily active window: it dials only between
``start_min`` and ``end_min`` (minutes since local midnight, where "local" is
the campaign's ``tz_offset`` hours from UTC). Outside the window the LoopEngine
pauses the dialer (stops the UAC) and resumes it when the window reopens; the
campaign row stays ``running`` throughout, so a restart inside a quiet period
comes back correctly paused.

This module is pure so both the engine and its tests agree to the minute:
``in_window`` is the whole contract.

Semantics:
  * start == end            -> always on (a zero-length window would never dial;
                               treat "no distinction" as 24h to avoid a footgun).
  * start <  end            -> daytime window [start, end)  (e.g. 08:00–20:00).
  * start >  end            -> overnight window that wraps midnight
                               (e.g. 22:00–06:00 dials across midnight).
Endpoints are half-open [start, end): a call minute equal to end_min is OUTSIDE.
"""

MINUTES_PER_DAY = 24 * 60


def normalize_minute(value: int) -> int:
    """Clamp an arbitrary integer into [0, 1439] (minute of day)."""
    return int(value) % MINUTES_PER_DAY


def local_minute_of_day(utc_epoch_s: float, tz_offset_hours: int) -> int:
    """Minute-of-day in the campaign's local zone for a UTC epoch second.

    tz_offset is whole hours east of UTC (matches the diurnal profile's
    tz_offset). Kept integer-only so it is deterministic and testable without a
    timezone database.
    """
    local_s = utc_epoch_s + int(tz_offset_hours) * 3600
    return int((local_s // 60) % MINUTES_PER_DAY)


def in_window(now_minute: int, start_min: int, end_min: int) -> bool:
    """True if ``now_minute`` is inside the daily window (half-open [start,end))."""
    start = normalize_minute(start_min)
    end = normalize_minute(end_min)
    now = normalize_minute(now_minute)
    if start == end:
        return True                      # no distinction -> always on
    if start < end:
        return start <= now < end        # same-day window
    return now >= start or now < end     # overnight wrap


def is_active(utc_epoch_s: float, start_min: int, end_min: int,
             tz_offset_hours: int) -> bool:
    """Convenience: is the campaign inside its window at this UTC instant?"""
    return in_window(
        local_minute_of_day(utc_epoch_s, tz_offset_hours), start_min, end_min)


def format_window(start_min: int, end_min: int) -> str:
    """Human "HH:MM–HH:MM" for logs/UI."""
    def hhmm(m):
        m = normalize_minute(m)
        return f"{m // 60:02d}:{m % 60:02d}"
    return f"{hhmm(start_min)}–{hhmm(end_min)}"
