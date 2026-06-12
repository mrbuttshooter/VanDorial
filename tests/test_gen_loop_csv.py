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
        count=50, seed=1)
    assert len(pairs) == 50
    orange = set(zones["Guinea-Mobile (Orange)"])
    for a, b in pairs:
        assert a.isdigit() and b.isdigit()
        # Valid E.164 lengths by country (Nigeria 234 -> 13, Guinea 224 -> 12),
        # NOT a flat length — a wrong-length dialed number is what MADA 404'd.
        assert len(a) == 13 and len(b) == 12
        assert a.startswith("2341")                       # oad zone code
        assert any(b.startswith(c) for c in orange)       # one of the dad codes


def test_e164_length_by_country_and_override():
    assert g.e164_total_length("22462", 11) == 12        # Guinea
    assert g.e164_total_length("2341", 11) == 13         # Nigeria
    assert g.e164_total_length("99999", 11) == 11        # unknown -> fallback


def test_per_side_length_override(zones):
    # Force Lagos A to 11 (a Lagos landline) while Guinea B stays E.164 (12).
    pairs = g.generate_pairs(
        zones, oad_zone="Nigeria-Lagos", dad_code="22462",
        count=10, seed=2, oad_length=11)
    for a, b in pairs:
        assert len(a) == 11 and a.startswith("2341")
        assert len(b) == 12 and b.startswith("22462")


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


def test_write_csv_default_is_bare_a_b_rows():
    """Default output is one `A;B` pair per line — no header, no trailing ';'."""
    buf = io.StringIO()
    g.write_csv([("44750348677", "2427070364797")], buf)
    assert buf.getvalue() == "44750348677;2427070364797\n"


def test_write_csv_order_header_optional():
    buf = io.StringIO()
    g.write_csv([("2341000", "2246200")], buf, order="random")
    lines = buf.getvalue().splitlines()
    assert lines == ["RANDOM", "2341000;2246200"]


def test_derive_country_handles_breakouts_and_dash_names():
    assert g.derive_country("Nigeria-Lagos") == "Nigeria"
    assert g.derive_country("Guinea-Mobile (Orange)") == "Guinea"
    assert g.derive_country("Nigeria") == "Nigeria"
    assert g.derive_country("Bosnia & Herzegovina (BH Telecom)") == "Bosnia & Herzegovina"
    # dash-named country kept whole
    assert g.derive_country("Guinea-Bissau-Mobile") == "Guinea-Bissau"


def test_build_country_tree_groups_zones(zones):
    tree = g.build_country_tree(zones)
    assert "Nigeria" in tree and "Guinea" in tree
    assert "Nigeria-Lagos" in tree["Nigeria"]
    assert "Guinea-Mobile (Orange)" in tree["Guinea"]
    # Guinea base must NOT swallow Nigeria zones
    assert all(z.startswith("Guinea") for z in tree["Guinea"])


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
    assert len(lines) == 5  # no header, 5 rows
    for row in lines:
        a, b = row.split(";")
        assert a.startswith("2341")
        assert b.isdigit()
