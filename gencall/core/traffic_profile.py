"""Diurnal traffic profile: a 24h attempt-weight curve + the sizing math that
turns a daily minutes target + ACD into per-hour CPS, peak CPS and peak
concurrency. Pure (no I/O / DB) so the Calculator API and the runtime shaper
compute identically."""
import math

PRESETS = ("diurnal",)

_CURVE_DEFAULTS = dict(
    night_floor=0.25, ramp_up_start=6, plateau_start=9,
    plateau_end=18, ramp_down_end=22, tz_offset=0,
)


def make_curve(preset="diurnal", *, night_floor=0.25, ramp_up_start=6,
               plateau_start=9, plateau_end=18, ramp_down_end=22,
               tz_offset=0):
    """Return 24 relative attempt weights (peak 1.0, night = night_floor).

    Trapezoid: night_floor overnight -> linear ramp up to 1.0 -> plateau ->
    linear ramp down to night_floor. ``tz_offset`` rotates the array so a box at
    hour ``t`` uses the destination market's local-hour weight: w[t] =
    base[(t + tz_offset) % 24]."""
    nf = max(0.0, min(1.0, float(night_floor)))
    rus, ps = int(ramp_up_start), int(plateau_start)
    pe, rde = int(plateau_end), int(ramp_down_end)
    base = []
    for h in range(24):
        if h < rus or h >= rde:
            v = nf
        elif rus <= h < ps:
            v = nf + (1.0 - nf) * (h - rus) / max(1, ps - rus)
        elif ps <= h <= pe:
            v = 1.0
        else:  # pe < h < rde
            v = 1.0 - (1.0 - nf) * (h - pe) / max(1, rde - pe)
        base.append(round(v, 4))
    off = int(tz_offset) % 24
    return [base[(h + off) % 24] for h in range(24)] if off else base


def calculate(target_minutes, acd_s, profile=None, *,
              max_cps=None, max_channels=None):
    """Size a diurnal campaign. ``profile`` is the make_curve kwargs (preset +
    knobs). Returns per-hour CPS, peak/avg CPS, peak concurrency, and (when caps
    are given) warnings + nodes_needed. No ASR: assumes ~100% answer (the loop
    UAS auto-answers), so attempts == answered for sizing."""
    acd_s = float(acd_s)
    if acd_s <= 0:
        raise ValueError("acd_s must be > 0")
    if float(target_minutes) < 0:
        raise ValueError("target_minutes must be >= 0")
    curve = make_curve(**(profile or {}))
    total = sum(curve) or 1.0
    attempts_per_day = float(target_minutes) * 60.0 / acd_s     # ~100% answer
    per_hour = []
    for h in range(24):
        attempts = attempts_per_day * curve[h] / total
        per_hour.append({"hour": h, "weight": curve[h],
                         "cps": round(attempts / 3600.0, 3),
                         "attempts": int(round(attempts))})
    peak_cps = max((x["cps"] for x in per_hour), default=0.0)
    avg_cps = attempts_per_day / 86400.0
    peak_concurrent = math.ceil(peak_cps * acd_s * 1.2)
    warnings, nodes_needed = [], 1
    if max_cps and peak_cps > max_cps:
        n = math.ceil(peak_cps / max_cps)
        nodes_needed = max(nodes_needed, n)
        warnings.append(f"peak {peak_cps} cps exceeds the {max_cps} cps cap — "
                        f"split across {n} nodes")
    if max_channels and peak_concurrent > max_channels:
        n = math.ceil(peak_concurrent / max_channels)
        nodes_needed = max(nodes_needed, n)
        warnings.append(f"peak {peak_concurrent} concurrent exceeds the "
                        f"{max_channels} channel cap — split across {n} nodes")
    return {
        "per_hour": per_hour,
        "peak_cps": round(peak_cps, 3),
        "avg_cps": round(avg_cps, 3),
        "peak_concurrent": peak_concurrent,
        "attempts_per_day": int(round(attempts_per_day)),
        "warnings": warnings,
        "nodes_needed": nodes_needed,
    }
