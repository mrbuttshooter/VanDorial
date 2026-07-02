from gencall.core import traffic_profile as tp


def test_make_curve_shape_defaults():
    w = tp.make_curve()
    assert len(w) == 24
    assert max(w) == 1.0                      # plateau peaks at 1.0
    assert w[2] == 0.25                        # 02:00 sits at night_floor
    assert w[12] == 1.0                        # midday on the plateau
    # monotonic ramp up across the morning
    assert w[6] <= w[7] <= w[8] <= w[9] == 1.0


def test_make_curve_tz_offset_rotates():
    base = tp.make_curve(tz_offset=0)
    rot = tp.make_curve(tz_offset=3)
    assert rot[0] == base[3 % 24]
    assert rot[12] == base[15]


def test_calculate_minutes_to_peak_cps_and_concurrent():
    # 1,000,000 min/day, ACD 120s -> 500,000 answered calls/day (~=attempts)
    r = tp.calculate(target_minutes=1_000_000, acd_s=120, profile={})
    assert r["attempts_per_day"] == 500_000
    # average cps = 500000/86400 ~= 5.79; peak is higher (diurnal concentration)
    assert abs(r["avg_cps"] - 500_000 / 86400) < 0.01
    assert r["peak_cps"] > r["avg_cps"]
    # peak concurrent = ceil(peak_cps * acd * 1.2)
    import math
    assert r["peak_concurrent"] == math.ceil(r["peak_cps"] * 120 * 1.2)
    # per-hour attempts sum back to the daily total (within rounding)
    assert abs(sum(h["attempts"] for h in r["per_hour"]) - 500_000) <= 24


def test_calculate_caps_warn_and_suggest_nodes():
    r = tp.calculate(target_minutes=5_000_000, acd_s=60, profile={},
                     max_cps=500, max_channels=1000)
    assert r["warnings"]                         # peak exceeds caps
    assert r["nodes_needed"] >= 2


def test_calculate_rejects_bad_acd():
    import pytest
    with pytest.raises(ValueError):
        tp.calculate(target_minutes=1000, acd_s=0, profile={})
