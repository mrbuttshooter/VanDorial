"""
GenCall Geographic Traffic Simulator.

Simulates calls originating from different geographic regions, each with
realistic network characteristics (latency, jitter, packet loss),
busy-hour traffic patterns, codec preferences, and caller-ID prefixes.

Supports mixing multiple regions in a single test with configurable
ratios and time-zone-aware traffic shaping.
"""

from __future__ import annotations

import datetime
import logging
import math
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("gencall.geo_traffic")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class TrafficIntensity(Enum):
    OFF_PEAK = "off_peak"
    SHOULDER = "shoulder"
    BUSINESS = "business"
    PEAK = "peak"


@dataclass
class NetworkProfile:
    """Network characteristics for a geographic region."""
    min_latency_ms: float = 10.0
    max_latency_ms: float = 50.0
    min_jitter_ms: float = 1.0
    max_jitter_ms: float = 10.0
    packet_loss_pct: float = 0.5
    bandwidth_kbps: float = 10000.0

    def sample_latency(self) -> float:
        """Sample a latency value from this profile's range."""
        return random.uniform(self.min_latency_ms, self.max_latency_ms)

    def sample_jitter(self) -> float:
        return random.uniform(self.min_jitter_ms, self.max_jitter_ms)

    def sample_loss(self) -> bool:
        return random.random() * 100.0 < self.packet_loss_pct

    def to_dict(self) -> dict:
        return {
            "min_latency_ms": self.min_latency_ms,
            "max_latency_ms": self.max_latency_ms,
            "min_jitter_ms": self.min_jitter_ms,
            "max_jitter_ms": self.max_jitter_ms,
            "packet_loss_pct": self.packet_loss_pct,
            "bandwidth_kbps": self.bandwidth_kbps,
        }


@dataclass
class BusyHourProfile:
    """Defines traffic intensity across a 24-hour day (hour 0-23)."""
    # Mapping from hour -> relative intensity (0.0 - 1.0)
    hourly_weights: list[float] = field(default_factory=lambda: [0.5] * 24)

    def intensity_at(self, hour: int) -> float:
        """Return the traffic intensity for a given hour (0-23)."""
        return self.hourly_weights[hour % 24]

    def current_intensity(self, tz: ZoneInfo) -> float:
        """Get intensity for the current hour in the given timezone."""
        now = datetime.datetime.now(tz)
        return self.intensity_at(now.hour)

    def classify(self, hour: int) -> TrafficIntensity:
        w = self.intensity_at(hour)
        if w >= 0.8:
            return TrafficIntensity.PEAK
        if w >= 0.5:
            return TrafficIntensity.BUSINESS
        if w >= 0.2:
            return TrafficIntensity.SHOULDER
        return TrafficIntensity.OFF_PEAK

    def to_dict(self) -> dict:
        return {"hourly_weights": [round(w, 2) for w in self.hourly_weights]}


def _business_hours_profile() -> BusyHourProfile:
    """Standard business-hours profile (peak 9-17, ramp up/down around edges)."""
    weights = [0.0] * 24
    for h in range(24):
        if 9 <= h <= 17:
            weights[h] = 1.0
        elif h == 8 or h == 18:
            weights[h] = 0.6
        elif h == 7 or h == 19:
            weights[h] = 0.3
        elif h == 20:
            weights[h] = 0.15
        elif 6 <= h <= 21:
            weights[h] = 0.1
        else:
            weights[h] = 0.05
    return BusyHourProfile(hourly_weights=weights)


# ---------------------------------------------------------------------------
# Region profile
# ---------------------------------------------------------------------------

@dataclass
class RegionProfile:
    """Complete profile for a geographic region."""
    region_id: str = ""
    name: str = ""
    description: str = ""
    country_codes: list[str] = field(default_factory=list)
    timezone: str = "UTC"
    network: NetworkProfile = field(default_factory=NetworkProfile)
    busy_hours: BusyHourProfile = field(default_factory=_business_hours_profile)
    preferred_codecs: list[str] = field(default_factory=lambda: ["G.711a", "G.711u"])
    caller_id_prefixes: list[str] = field(default_factory=list)
    locale: str = "en"

    def __post_init__(self) -> None:
        if not self.region_id:
            self.region_id = self.name.lower().replace(" ", "_")

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except Exception:
            return ZoneInfo("UTC")

    @property
    def current_hour(self) -> int:
        return datetime.datetime.now(self.tz).hour

    @property
    def current_intensity(self) -> float:
        return self.busy_hours.current_intensity(self.tz)

    @property
    def current_traffic_class(self) -> TrafficIntensity:
        return self.busy_hours.classify(self.current_hour)

    def generate_caller_id(self) -> str:
        """Generate a realistic caller ID with region-appropriate prefix."""
        if not self.caller_id_prefixes:
            prefix = "+1"
        else:
            prefix = random.choice(self.caller_id_prefixes)
        # Generate remaining digits
        remaining = 10 - len(prefix.replace("+", ""))
        digits = "".join(str(random.randint(0, 9)) for _ in range(max(remaining, 7)))
        return f"{prefix}{digits}"

    def select_codec(self) -> str:
        """Select a codec based on region preferences."""
        if not self.preferred_codecs:
            return "G.711a"
        return random.choice(self.preferred_codecs)

    def to_dict(self) -> dict:
        return {
            "region_id": self.region_id,
            "name": self.name,
            "description": self.description,
            "country_codes": self.country_codes,
            "timezone": self.timezone,
            "current_hour": self.current_hour,
            "current_intensity": round(self.current_intensity, 2),
            "current_traffic_class": self.current_traffic_class.value,
            "network": self.network.to_dict(),
            "busy_hours": self.busy_hours.to_dict(),
            "preferred_codecs": self.preferred_codecs,
            "caller_id_prefixes": self.caller_id_prefixes,
            "locale": self.locale,
        }


# ---------------------------------------------------------------------------
# Built-in region profiles
# ---------------------------------------------------------------------------

REGION_PROFILES: dict[str, RegionProfile] = {
    "north_america": RegionProfile(
        name="North America",
        description="US & Canada - low latency, excellent infrastructure",
        country_codes=["US", "CA"],
        timezone="America/New_York",
        network=NetworkProfile(
            min_latency_ms=10.0, max_latency_ms=60.0,
            min_jitter_ms=1.0, max_jitter_ms=8.0,
            packet_loss_pct=0.3, bandwidth_kbps=50000.0,
        ),
        preferred_codecs=["G.711u", "G.711a", "G.729", "opus"],
        caller_id_prefixes=["+1212", "+1310", "+1415", "+1312", "+1416", "+1604"],
        locale="en-US",
    ),
    "europe": RegionProfile(
        name="Europe",
        description="Western/Central Europe - good infrastructure",
        country_codes=["GB", "DE", "FR", "NL", "ES", "IT"],
        timezone="Europe/London",
        network=NetworkProfile(
            min_latency_ms=15.0, max_latency_ms=80.0,
            min_jitter_ms=2.0, max_jitter_ms=12.0,
            packet_loss_pct=0.5, bandwidth_kbps=30000.0,
        ),
        preferred_codecs=["G.711a", "G.729", "opus"],
        caller_id_prefixes=["+44", "+49", "+33", "+31", "+34", "+39"],
        locale="en-GB",
    ),
    "middle_east": RegionProfile(
        name="Middle East",
        description="Gulf states and Levant - moderate infrastructure",
        country_codes=["AE", "SA", "QA", "KW", "BH", "JO", "IL"],
        timezone="Asia/Dubai",
        network=NetworkProfile(
            min_latency_ms=40.0, max_latency_ms=150.0,
            min_jitter_ms=5.0, max_jitter_ms=25.0,
            packet_loss_pct=1.5, bandwidth_kbps=10000.0,
        ),
        preferred_codecs=["G.729", "G.711a"],
        caller_id_prefixes=["+971", "+966", "+974", "+965", "+962"],
        locale="ar",
    ),
    "asia_pacific": RegionProfile(
        name="Asia-Pacific",
        description="Japan, Korea, Australia, SE Asia - varied infrastructure",
        country_codes=["JP", "KR", "AU", "SG", "IN", "TH", "PH"],
        timezone="Asia/Tokyo",
        network=NetworkProfile(
            min_latency_ms=30.0, max_latency_ms=180.0,
            min_jitter_ms=3.0, max_jitter_ms=20.0,
            packet_loss_pct=1.0, bandwidth_kbps=15000.0,
        ),
        preferred_codecs=["G.711a", "G.729", "opus", "iLBC"],
        caller_id_prefixes=["+81", "+82", "+61", "+65", "+91", "+66"],
        locale="ja",
    ),
    "africa": RegionProfile(
        name="Africa",
        description="Sub-Saharan Africa - challenging network conditions",
        country_codes=["ZA", "NG", "KE", "GH", "TZ", "ET"],
        timezone="Africa/Johannesburg",
        network=NetworkProfile(
            min_latency_ms=80.0, max_latency_ms=350.0,
            min_jitter_ms=10.0, max_jitter_ms=50.0,
            packet_loss_pct=3.0, bandwidth_kbps=5000.0,
        ),
        preferred_codecs=["G.729", "GSM"],
        caller_id_prefixes=["+27", "+234", "+254", "+233", "+255"],
        locale="en",
    ),
    "south_america": RegionProfile(
        name="South America",
        description="Brazil, Argentina, Chile - moderate conditions",
        country_codes=["BR", "AR", "CL", "CO", "PE"],
        timezone="America/Sao_Paulo",
        network=NetworkProfile(
            min_latency_ms=50.0, max_latency_ms=200.0,
            min_jitter_ms=5.0, max_jitter_ms=30.0,
            packet_loss_pct=2.0, bandwidth_kbps=8000.0,
        ),
        preferred_codecs=["G.711a", "G.729"],
        caller_id_prefixes=["+55", "+54", "+56", "+57", "+51"],
        locale="pt-BR",
    ),
}


# ---------------------------------------------------------------------------
# Traffic mix
# ---------------------------------------------------------------------------

@dataclass
class RegionWeight:
    """A region and its percentage in the traffic mix."""
    region_id: str = ""
    weight_pct: float = 0.0

    def to_dict(self) -> dict:
        return {
            "region_id": self.region_id,
            "weight_pct": round(self.weight_pct, 2),
        }


@dataclass
class TrafficMix:
    """Definition of how traffic is distributed across regions."""
    mix_id: str = ""
    name: str = ""
    regions: list[RegionWeight] = field(default_factory=list)
    time_aware: bool = True  # Adjust for busy-hour patterns

    def __post_init__(self) -> None:
        if not self.mix_id:
            self.mix_id = uuid.uuid4().hex[:10]

    def validate(self) -> bool:
        """Check that weights sum to 100 (within tolerance)."""
        total = sum(r.weight_pct for r in self.regions)
        return 99.0 <= total <= 101.0

    def normalise(self) -> None:
        """Adjust weights to sum exactly to 100."""
        total = sum(r.weight_pct for r in self.regions)
        if total > 0:
            for r in self.regions:
                r.weight_pct = (r.weight_pct / total) * 100.0

    def select_region(self) -> Optional[RegionProfile]:
        """Pick a region based on configured weights, optionally adjusted for busy hours."""
        if not self.regions:
            return None

        weights: list[float] = []
        profiles: list[RegionProfile] = []

        for rw in self.regions:
            profile = REGION_PROFILES.get(rw.region_id)
            if profile is None:
                continue
            w = rw.weight_pct
            if self.time_aware:
                # Scale weight by current busy-hour intensity
                w *= profile.current_intensity
            weights.append(max(w, 0.001))  # avoid zero
            profiles.append(profile)

        if not profiles:
            return None

        total = sum(weights)
        r = random.uniform(0, total)
        cumulative = 0.0
        for profile, w in zip(profiles, weights):
            cumulative += w
            if r <= cumulative:
                return profile

        return profiles[-1]

    def to_dict(self) -> dict:
        return {
            "mix_id": self.mix_id,
            "name": self.name,
            "regions": [r.to_dict() for r in self.regions],
            "time_aware": self.time_aware,
            "valid": self.validate(),
        }


# Pre-built mixes

GLOBAL_MIX = TrafficMix(
    name="Global Default",
    regions=[
        RegionWeight(region_id="north_america", weight_pct=40.0),
        RegionWeight(region_id="europe", weight_pct=30.0),
        RegionWeight(region_id="asia_pacific", weight_pct=20.0),
        RegionWeight(region_id="middle_east", weight_pct=4.0),
        RegionWeight(region_id="south_america", weight_pct=4.0),
        RegionWeight(region_id="africa", weight_pct=2.0),
    ],
)


# ---------------------------------------------------------------------------
# Simulated call
# ---------------------------------------------------------------------------

@dataclass
class GeoSimulatedCall:
    """A single call generated by the geographic simulator."""
    call_id: str = ""
    region_id: str = ""
    region_name: str = ""
    caller_id: str = ""
    callee: str = ""
    codec: str = ""
    latency_ms: float = 0.0
    jitter_ms: float = 0.0
    packet_loss_pct: float = 0.0
    traffic_class: str = ""
    local_hour: int = 0
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.call_id:
            self.call_id = uuid.uuid4().hex[:12]

    def to_dict(self) -> dict:
        return {
            "call_id": self.call_id,
            "region_id": self.region_id,
            "region_name": self.region_name,
            "caller_id": self.caller_id,
            "callee": self.callee,
            "codec": self.codec,
            "latency_ms": round(self.latency_ms, 2),
            "jitter_ms": round(self.jitter_ms, 2),
            "packet_loss_pct": round(self.packet_loss_pct, 2),
            "traffic_class": self.traffic_class,
            "local_hour": self.local_hour,
            "timestamp": round(self.timestamp, 3),
        }


# ---------------------------------------------------------------------------
# Region statistics
# ---------------------------------------------------------------------------

@dataclass
class RegionStats:
    """Aggregated statistics per region."""
    region_id: str = ""
    region_name: str = ""
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    avg_latency_ms: float = 0.0
    avg_jitter_ms: float = 0.0
    avg_packet_loss_pct: float = 0.0
    avg_mos: float = 0.0
    codecs_used: dict[str, int] = field(default_factory=dict)

    def record_call(
        self,
        success: bool,
        latency_ms: float,
        jitter_ms: float,
        loss_pct: float,
        mos: float,
        codec: str,
    ) -> None:
        self.total_calls += 1
        if success:
            self.successful_calls += 1
        else:
            self.failed_calls += 1

        n = self.total_calls
        self.avg_latency_ms = self.avg_latency_ms + (latency_ms - self.avg_latency_ms) / n
        self.avg_jitter_ms = self.avg_jitter_ms + (jitter_ms - self.avg_jitter_ms) / n
        self.avg_packet_loss_pct = self.avg_packet_loss_pct + (loss_pct - self.avg_packet_loss_pct) / n
        self.avg_mos = self.avg_mos + (mos - self.avg_mos) / n
        self.codecs_used[codec] = self.codecs_used.get(codec, 0) + 1

    @property
    def success_rate(self) -> float:
        if self.total_calls == 0:
            return 0.0
        return (self.successful_calls / self.total_calls) * 100.0

    def to_dict(self) -> dict:
        return {
            "region_id": self.region_id,
            "region_name": self.region_name,
            "total_calls": self.total_calls,
            "successful_calls": self.successful_calls,
            "failed_calls": self.failed_calls,
            "success_rate": round(self.success_rate, 2),
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "avg_jitter_ms": round(self.avg_jitter_ms, 2),
            "avg_packet_loss_pct": round(self.avg_packet_loss_pct, 2),
            "avg_mos": round(self.avg_mos, 2),
            "codecs_used": self.codecs_used,
        }


# ---------------------------------------------------------------------------
# Geo Traffic Simulator
# ---------------------------------------------------------------------------

class GeoTrafficSimulator:
    """
    Geographic traffic simulation engine.

    Generates calls with region-appropriate characteristics.
    Integrates with the SIPp engine and network simulator to apply
    realistic per-call impairments.

    Usage::

        sim = GeoTrafficSimulator(mix=GLOBAL_MIX)
        call = sim.generate_call(callee="sip:test@pbx.local")
        # call.latency_ms, call.caller_id, etc. are region-appropriate
    """

    def __init__(
        self,
        mix: Optional[TrafficMix] = None,
        custom_regions: Optional[dict[str, RegionProfile]] = None,
    ) -> None:
        self._mix = mix or GLOBAL_MIX
        self._lock = threading.Lock()
        self._stats: dict[str, RegionStats] = {}
        self._call_count = 0

        # Register custom regions
        if custom_regions:
            for rid, profile in custom_regions.items():
                REGION_PROFILES[rid] = profile

        logger.info(
            "GeoTrafficSimulator initialized with mix '%s' (%d regions)",
            self._mix.name, len(self._mix.regions),
        )

    @property
    def mix(self) -> TrafficMix:
        return self._mix

    @mix.setter
    def mix(self, value: TrafficMix) -> None:
        self._mix = value
        logger.info("Traffic mix changed to: %s", value.name)

    # -- Call generation ---------------------------------------------------

    def generate_call(self, callee: str = "") -> GeoSimulatedCall:
        """Generate a single call from a randomly selected region."""
        region = self._mix.select_region()
        if region is None:
            region = REGION_PROFILES.get("north_america", RegionProfile(name="Default"))

        call = GeoSimulatedCall(
            region_id=region.region_id,
            region_name=region.name,
            caller_id=region.generate_caller_id(),
            callee=callee,
            codec=region.select_codec(),
            latency_ms=region.network.sample_latency(),
            jitter_ms=region.network.sample_jitter(),
            packet_loss_pct=region.network.packet_loss_pct,
            traffic_class=region.current_traffic_class.value,
            local_hour=region.current_hour,
        )

        with self._lock:
            self._call_count += 1

        return call

    def generate_batch(self, count: int, callee: str = "") -> list[GeoSimulatedCall]:
        """Generate a batch of calls distributed across regions."""
        return [self.generate_call(callee) for _ in range(count)]

    # -- Statistics --------------------------------------------------------

    def record_result(
        self,
        call: GeoSimulatedCall,
        success: bool,
        mos: float = 0.0,
    ) -> None:
        """Record the result of a simulated call for per-region stats."""
        with self._lock:
            if call.region_id not in self._stats:
                self._stats[call.region_id] = RegionStats(
                    region_id=call.region_id,
                    region_name=call.region_name,
                )
            self._stats[call.region_id].record_call(
                success=success,
                latency_ms=call.latency_ms,
                jitter_ms=call.jitter_ms,
                loss_pct=call.packet_loss_pct,
                mos=mos,
                codec=call.codec,
            )

    def get_stats(self) -> list[dict]:
        """Get per-region statistics."""
        with self._lock:
            return [s.to_dict() for s in self._stats.values()]

    def get_region_stats(self, region_id: str) -> Optional[dict]:
        with self._lock:
            s = self._stats.get(region_id)
            return s.to_dict() if s else None

    def reset_stats(self) -> None:
        with self._lock:
            self._stats.clear()
            self._call_count = 0

    # -- Current conditions ------------------------------------------------

    def get_current_conditions(self) -> list[dict]:
        """Show current time-of-day and traffic intensity for all mix regions."""
        result: list[dict] = []
        for rw in self._mix.regions:
            profile = REGION_PROFILES.get(rw.region_id)
            if profile is None:
                continue
            now = datetime.datetime.now(profile.tz)
            result.append({
                "region_id": profile.region_id,
                "region_name": profile.name,
                "local_time": now.strftime("%H:%M:%S"),
                "local_hour": now.hour,
                "timezone": profile.timezone,
                "intensity": round(profile.current_intensity, 2),
                "traffic_class": profile.current_traffic_class.value,
                "weight_pct": rw.weight_pct,
                "effective_weight": round(rw.weight_pct * profile.current_intensity, 2),
            })
        return result

    @staticmethod
    def list_regions() -> list[dict]:
        """List all available region profiles."""
        return [p.to_dict() for p in REGION_PROFILES.values()]

    @staticmethod
    def get_region(region_id: str) -> Optional[dict]:
        p = REGION_PROFILES.get(region_id)
        return p.to_dict() if p else None

    # -- MOS estimation ----------------------------------------------------

    @staticmethod
    def estimate_mos(latency_ms: float, jitter_ms: float, loss_pct: float) -> float:
        """
        Estimate MOS from network conditions using a simplified E-model.

        Based on ITU-T G.107 simplified approach:
        R = 93.2 - Id - Ie
        Where Id = delay impairment, Ie = equipment (loss) impairment
        MOS = 1 + 0.035*R + R*(R-60)*(100-R)*7e-6
        """
        # Delay impairment
        effective_delay = latency_ms + jitter_ms * 2
        if effective_delay < 160:
            id_factor = 0.024 * effective_delay
        else:
            id_factor = 0.024 * effective_delay + 0.11 * (effective_delay - 160)
        id_factor = min(id_factor, 50.0)

        # Equipment impairment (packet loss)
        ie_factor = 0.0
        if loss_pct > 0:
            ie_factor = 30.0 * math.log(1.0 + 15.0 * loss_pct)
        ie_factor = min(ie_factor, 80.0)

        r_factor = 93.2 - id_factor - ie_factor
        r_factor = max(0.0, min(100.0, r_factor))

        if r_factor < 0:
            mos = 1.0
        elif r_factor > 100:
            mos = 4.5
        else:
            mos = 1.0 + 0.035 * r_factor + r_factor * (r_factor - 60) * (100 - r_factor) * 7e-6
        return max(1.0, min(5.0, round(mos, 2)))

    # -- Serialisation -----------------------------------------------------

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "mix": self._mix.to_dict(),
                "total_calls_generated": self._call_count,
                "region_stats": [s.to_dict() for s in self._stats.values()],
                "current_conditions": self.get_current_conditions(),
            }
