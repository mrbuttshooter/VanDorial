"""
Tests for the sale-zone loop CSV generator (gencall/scripts/gen_loop_csv.py).

These run against the committed sample deck (sale_codes.sample.csv) so they need
neither the proprietary full deck nor openpyxl.
"""

import io
import os
import re

import pytest

from gencall.scripts import gen_loop_csv as g

SAMPLE = os.path.join(os.path.dirname(g.__file__), "data", "sale_codes.sample.csv")


@pytest.fixture
def zones():
    return g.load_zones(SAMPLE)


def test_sample_deck_loads_expected_zones(zones):
    assert "Nigeria-Lagos" in zones
    assert zones["Nigeria-Lagos"] == ["2341"]
    # multi-code zone, kept shortest-first
    orange = zones["Guinea-Mobile (Orange)"]
    assert orange[0] == "22462"
    assert set(orange) == {"22462", "224720", "224721"}


def test_find_zone_exact_and_substring(zones):
    assert g.find_zone(zones, "nigeria-lagos") == "Nigeria-Lagos"   # case-insensitive exact
    assert g.find_zone(zones, "orange") == "Guinea-Mobile (Orange)"  # unique substring


def test_find_zone_ambiguous_raises(zones):
    with pytest.raises(ValueError, match="ambiguous"):
        g.find_zone(zones, "nigeria-mobile")  # Airtel + MTN, no exact match


def test_find_zone_absent_raises(zones):
    with pytest.raises(ValueError, match="no zone matches"):
        g.find_zone(zones, "atlantis")


def test_zone_pairs_start_with_zone_codes(zones):
    pairs = g.generate_pairs(
        zones, oad_zone="Nigeria-Lagos", dad_zone="Guinea-Mobile (Orange)",
        count=50, length=11, seed=1)
    assert len(pairs) == 50
    orange = set(zones["Guinea-Mobile (Orange)"])
    for a, b in pairs:
        assert a.isdigit() and b.isdigit()
        assert len(a) == 11 and len(b) == 11
        assert a.startswith("2341")                       # oad zone code
        assert any(b.startswith(c) for c in orange)       # one of the dad codes


def test_pin_dad_code_uses_only_that_code(zones):
    pairs = g.generate_pairs(
        zones, oad_zone="Nigeria-Lagos", dad_zone="Guinea-Mobile (Orange)",
        dad_code="22462", count=30, length=11, seed=2)
    assert all(b.startswith("22462") for _, b in pairs)


def test_min_subscriber_digits_enforced_for_long_code(zones):
    # length smaller than code+min_sub must still yield code+>=4 subscriber digits
    pairs = g.generate_pairs(
        zones, oad_code="2341", dad_code="224720", count=5, length=4, seed=3)
    for _, b in pairs:
        assert b.startswith("224720")
        assert len(b) >= len("224720") + 4


def test_seed_is_reproducible(zones):
    kw = dict(oad_zone="Nigeria-Lagos", dad_zone="Guinea-Mobile (Orange)",
              count=20, length=11, seed=42)
    assert g.generate_pairs(zones, **kw) == g.generate_pairs(zones, **kw)


def test_unique_pairs_by_default(zones):
    pairs = g.generate_pairs(
        zones, oad_zone="Nigeria-Lagos", dad_zone="Guinea-Mobile (Orange)",
        count=40, length=11, seed=7)
    assert len(set(pairs)) == len(pairs)


def test_write_csv_format_is_sipp_inf():
    buf = io.StringIO()
    g.write_csv([("2341000", "2246200")], buf)
    text = buf.getvalue()
    assert text.splitlines()[0] == "SEQUENTIAL"
    assert text.splitlines()[1] == "2341000;2246200;"


def test_pattern_path_still_matches_switch_regex(zones):
    # advanced fallback: raw NetAxis patterns still produce matching numbers
    pairs = g.generate_pairs(
        zones, oad_pattern=r"^....2341.*\|^....+2341.*",
        dad_pattern=r"^..22462.*\|^..+22462.*", count=10, length=12, seed=5)
    a_re = re.compile(g.translate_pattern(r"^....2341.*\|^....+2341.*"))
    b_re = re.compile(g.translate_pattern(r"^..22462.*\|^..+22462.*"))
    for a, b in pairs:
        assert a_re.match(a) and b_re.match(b)


def test_parse_skeleton_extracts_lead_and_token():
    assert g.parse_skeleton(r"^....2341.*\|^....+2341.*") == (4, "2341")
    assert g.parse_skeleton(r"^..22462.*") == (2, "22462")


def test_cli_list_zones(capsys):
    rc = g.main(["--codes", SAMPLE, "--list-zones", "guinea"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Guinea-Mobile (Orange)" in out


def test_cli_generates_to_file(tmp_path):
    out = tmp_path / "nums.csv"
    rc = g.main(["--codes", SAMPLE, "--oad-zone", "Nigeria-Lagos",
                 "--dad-zone", "Guinea-Mobile (Orange)", "--count", "5",
                 "--length", "11", "--seed", "1", "--out", str(out)])
    assert rc == 0
    lines = out.read_text().splitlines()
    assert lines[0] == "SEQUENTIAL"
    assert len(lines) == 6  # header + 5 rows
    for row in lines[1:]:
        a, b, _ = row.split(";")
        assert a.startswith("2341")
