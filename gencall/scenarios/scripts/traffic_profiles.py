"""
GenCall - Pre-built Traffic Profiles

Ready-to-use traffic patterns for different testing scenarios.
Import these in your outgoing_call.py or create your own.
"""

from outgoing_call import TrafficWindow


# ─── Call Center (9-5 heavy, evenings light) ──────────────────────────────────
CALL_CENTER = [
    TrafficWindow(0,  7,  0.02, 30,   60,   "Night - near zero"),
    TrafficWindow(7,  9,  0.40, 120,  240,  "Morning ramp"),
    TrafficWindow(9,  12, 0.95, 180,  600,  "Morning peak"),
    TrafficWindow(12, 13, 0.60, 120,  300,  "Lunch break"),
    TrafficWindow(13, 17, 0.95, 180,  600,  "Afternoon peak"),
    TrafficWindow(17, 19, 0.30, 60,   180,  "Wind down"),
    TrafficWindow(19, 24, 0.05, 30,   60,   "After hours"),
]

# ─── 24/7 Enterprise (consistent all day) ────────────────────────────────────
ENTERPRISE_24_7 = [
    TrafficWindow(0,  6,  0.40, 60,  300,  "Night shift"),
    TrafficWindow(6,  9,  0.70, 120, 400,  "Morning shift start"),
    TrafficWindow(9,  17, 0.85, 180, 600,  "Business hours"),
    TrafficWindow(17, 22, 0.65, 120, 400,  "Evening shift"),
    TrafficWindow(22, 24, 0.45, 60,  300,  "Late night"),
]

# ─── Residential / ISP (evenings heavy) ──────────────────────────────────────
RESIDENTIAL = [
    TrafficWindow(0,  7,  0.03, 30,   120,  "Night - minimal"),
    TrafficWindow(7,  9,  0.15, 60,   180,  "Morning - light"),
    TrafficWindow(9,  12, 0.20, 60,   240,  "Late morning"),
    TrafficWindow(12, 14, 0.25, 60,   180,  "Lunch time"),
    TrafficWindow(14, 17, 0.20, 60,   240,  "Afternoon"),
    TrafficWindow(17, 21, 0.80, 180,  900,  "Evening peak"),
    TrafficWindow(21, 24, 0.30, 120,  600,  "Late evening"),
]

# ─── Stress Test (max everything, always) ────────────────────────────────────
STRESS_TEST = [
    TrafficWindow(0, 24, 1.0, 5, 30, "Maximum load - always on"),
]

# ─── Burst Test (periodic bursts with quiet periods) ─────────────────────────
BURST_TEST = [
    TrafficWindow(0,  1,  1.0,  10, 30,  "Burst"),
    TrafficWindow(1,  2,  0.0,  0,  0,   "Quiet"),
    TrafficWindow(2,  3,  1.0,  10, 30,  "Burst"),
    TrafficWindow(3,  4,  0.0,  0,  0,   "Quiet"),
    TrafficWindow(4,  5,  1.0,  10, 30,  "Burst"),
    TrafficWindow(5,  6,  0.0,  0,  0,   "Quiet"),
    TrafficWindow(6,  7,  1.0,  10, 30,  "Burst"),
    TrafficWindow(7,  8,  0.0,  0,  0,   "Quiet"),
    TrafficWindow(8,  9,  1.0,  10, 30,  "Burst"),
    TrafficWindow(9,  10, 0.0,  0,  0,   "Quiet"),
    TrafficWindow(10, 11, 1.0,  10, 30,  "Burst"),
    TrafficWindow(11, 12, 0.0,  0,  0,   "Quiet"),
    TrafficWindow(12, 13, 1.0,  10, 30,  "Burst"),
    TrafficWindow(13, 14, 0.0,  0,  0,   "Quiet"),
    TrafficWindow(14, 15, 1.0,  10, 30,  "Burst"),
    TrafficWindow(15, 16, 0.0,  0,  0,   "Quiet"),
    TrafficWindow(16, 17, 1.0,  10, 30,  "Burst"),
    TrafficWindow(17, 18, 0.0,  0,  0,   "Quiet"),
    TrafficWindow(18, 19, 1.0,  10, 30,  "Burst"),
    TrafficWindow(19, 20, 0.0,  0,  0,   "Quiet"),
    TrafficWindow(20, 21, 1.0,  10, 30,  "Burst"),
    TrafficWindow(21, 22, 0.0,  0,  0,   "Quiet"),
    TrafficWindow(22, 23, 1.0,  10, 30,  "Burst"),
    TrafficWindow(23, 24, 0.0,  0,  0,   "Quiet"),
]

# ─── Gradual Ramp (for capacity testing) ─────────────────────────────────────
GRADUAL_RAMP = [
    TrafficWindow(0,  3,  0.10, 10, 30,   "10% load"),
    TrafficWindow(3,  6,  0.25, 30, 60,   "25% load"),
    TrafficWindow(6,  9,  0.50, 60, 120,  "50% load"),
    TrafficWindow(9,  12, 0.75, 90, 180,  "75% load"),
    TrafficWindow(12, 15, 1.00, 120, 240, "100% load"),
    TrafficWindow(15, 18, 0.75, 90, 180,  "75% load - ramp down"),
    TrafficWindow(18, 21, 0.50, 60, 120,  "50% load"),
    TrafficWindow(21, 24, 0.25, 30, 60,   "25% load"),
]

# ─── Profile Registry ────────────────────────────────────────────────────────
PROFILES = {
    "call_center": CALL_CENTER,
    "enterprise_24_7": ENTERPRISE_24_7,
    "residential": RESIDENTIAL,
    "stress_test": STRESS_TEST,
    "burst_test": BURST_TEST,
    "gradual_ramp": GRADUAL_RAMP,
}


def get_profile(name: str) -> list[TrafficWindow]:
    """Get a traffic profile by name."""
    if name not in PROFILES:
        available = ", ".join(PROFILES.keys())
        raise ValueError(f"Unknown profile '{name}'. Available: {available}")
    return PROFILES[name]
