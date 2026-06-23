"""Auto-timezone offset for the diurnal shaper (v2.2.3).

A campaign's diurnal curve is anchored to GMT; ``tz_offset`` rotates it to the
destination market's local daypart. The offset is derived automatically from the
node's drop zone/country so an operator never types it by hand. Whole-hour
offsets only (the curve rotates by whole hours); unknown or half-hour countries
fall back to the caller-supplied value (the preset's manual tz_offset).
"""
import pytest

from gencall.core import tz_offsets


@pytest.mark.parametrize("country,expected", [
    ("Iraq", 3), ("iraq", 3), ("  Iraq  ", 3),
    ("Algeria", 1), ("Morocco", 1), ("Nigeria", 1),
    ("Guinea", 0), ("Guinea-Bissau", 0),
    ("Egypt", 2), ("Saudi Arabia", 3),
    ("United Arab Emirates", 4), ("UAE", 4),
])
def test_country_utc_offset_known(country, expected):
    assert tz_offsets.country_utc_offset(country) == expected


@pytest.mark.parametrize("country", ["", "   ", "Atlantis", "Neverland", None])
def test_country_utc_offset_unknown_is_none(country):
    assert tz_offsets.country_utc_offset(country) is None


def test_offset_for_zone_plain_country():
    assert tz_offsets.offset_for_zone("Iraq", fallback=0) == 3


def test_offset_for_zone_strips_operator_and_breakout():
    # derive_country turns "Iraq-Mobile (Zain)" -> "Iraq", "Nigeria-Lagos" -> "Nigeria".
    assert tz_offsets.offset_for_zone("Iraq-Mobile (Zain)", fallback=0) == 3
    assert tz_offsets.offset_for_zone("Nigeria-Lagos", fallback=0) == 1


def test_offset_for_zone_dash_named_country():
    assert tz_offsets.offset_for_zone("Guinea-Bissau-Mobile", fallback=9) == 0


def test_offset_for_zone_unknown_country_uses_fallback():
    assert tz_offsets.offset_for_zone("Atlantis", fallback=7) == 7


def test_offset_for_zone_blank_zone_uses_fallback():
    assert tz_offsets.offset_for_zone("", fallback=2) == 2
    assert tz_offsets.offset_for_zone(None, fallback=2) == 2


def test_offset_for_zone_explicit_country_overrides_zone():
    # DB-overlay zones carry an explicit country; it wins over the derived name.
    assert tz_offsets.offset_for_zone("Wonderland", fallback=0, country="Iraq") == 3
