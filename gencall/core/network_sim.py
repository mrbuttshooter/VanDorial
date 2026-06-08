"""
GenCall Network Impairment Simulator.
Simulates real-world network conditions (packet loss, jitter, delay,
bandwidth throttling) on RTP streams using configurable models.
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger("gencall.network_sim")


# ─── Loss Models ──────────────────────────────────────────────────────────────

class LossModel(Enum):
    NONE = "none"
    RANDOM = "random"
    BURST = "burst"
    GILBERT_ELLIOTT = "gilbert_elliott"


@dataclass
class RandomLossConfig:
    """Simple i.i.d. packet loss."""
    rate: float = 0.0  # 0.0 - 1.0

    def should_drop(self) -> bool:
        return random.random() < self.rate

    def to_dict(self) -> dict:
        return {"model": "random", "rate": self.rate}


@dataclass
class BurstLossConfig:
    """Burst loss: drops N consecutive packets every M packets."""
    burst_length: int = 3
    burst_interval: int = 100
    _counter: int = field(default=0, repr=False)

    def should_drop(self) -> bool:
        self._counter += 1
        cycle_pos = self._counter % self.burst_interval
        return cycle_pos < self.burst_length

    def reset(self) -> None:
        self._counter = 0

    def to_dict(self) -> dict:
        return {
            "model": "burst",
            "burst_length": self.burst_length,
            "burst_interval": self.burst_interval,
        }


@dataclass
class GilbertElliottConfig:
    """
    Gilbert-Elliott two-state Markov loss model.
    State G (good): low loss probability p_g
    State B (bad): high loss probability p_b
    Transitions: G->B with probability p_gb, B->G with probability p_bg
    """
    p_g: float = 0.01    # loss probability in good state
    p_b: float = 0.30    # loss probability in bad state
    p_gb: float = 0.05   # transition good -> bad
    p_bg: float = 0.20   # transition bad -> good
    _in_bad_state: bool = field(default=False, repr=False)

    def should_drop(self) -> bool:
        # State transition
        if self._in_bad_state:
            if random.random() < self.p_bg:
                self._in_bad_state = False
        else:
            if random.random() < self.p_gb:
                self._in_bad_state = True

        loss_prob = self.p_b if self._in_bad_state else self.p_g
        return random.random() < loss_prob

    def reset(self) -> None:
        self._in_bad_state = False

    def to_dict(self) -> dict:
        return {
            "model": "gilbert_elliott",
            "p_g": self.p_g,
            "p_b": self.p_b,
            "p_gb": self.p_gb,
            "p_bg": self.p_bg,
            "in_bad_state": self._in_bad_state,
        }


# ─── Jitter Models ───────────────────────────────────────────────────────────

class JitterModel(Enum):
    NONE = "none"
    UNIFORM = "uniform"
    NORMAL = "normal"


@dataclass
class JitterConfig:
    """Jitter configuration for delay variation."""
    model: JitterModel = JitterModel.NONE
    min_ms: float = 0.0
    max_ms: float = 0.0
    mean_ms: float = 0.0     # for normal distribution
    stddev_ms: float = 0.0   # for normal distribution

    def compute_jitter(self) -> float:
        """Returns jitter in milliseconds (always >= 0)."""
        if self.model == JitterModel.UNIFORM:
            return random.uniform(self.min_ms, self.max_ms)
        elif self.model == JitterModel.NORMAL:
            return max(0.0, random.gauss(self.mean_ms, self.stddev_ms))
        return 0.0

    def to_dict(self) -> dict:
        return {
            "model": self.model.value,
            "min_ms": self.min_ms,
            "max_ms": self.max_ms,
            "mean_ms": self.mean_ms,
            "stddev_ms": self.stddev_ms,
        }


# ─── Delay Config ─────────────────────────────────────────────────────────────

class DelayModel(Enum):
    NONE = "none"
    FIXED = "fixed"
    VARIABLE = "variable"


@dataclass
class DelayConfig:
    """Network delay simulation."""
    model: DelayModel = DelayModel.NONE
    fixed_ms: float = 0.0
    min_ms: float = 0.0
    max_ms: float = 0.0

    def compute_delay(self) -> float:
        """Returns delay in milliseconds (always >= 0)."""
        if self.model == DelayModel.FIXED:
            return self.fixed_ms
        elif self.model == DelayModel.VARIABLE:
            return random.uniform(self.min_ms, self.max_ms)
        return 0.0

    def to_dict(self) -> dict:
        return {
            "model": self.model.value,
            "fixed_ms": self.fixed_ms,
            "min_ms": self.min_ms,
            "max_ms": self.max_ms,
        }


# ─── Bandwidth Throttle ──────────────────────────────────────────────────────

@dataclass
class BandwidthConfig:
    """Bandwidth throttling via token bucket algorithm."""
    enabled: bool = False
    rate_kbps: float = 0.0      # kilobits per second (0 = unlimited)
    burst_bytes: int = 0        # max burst size in bytes
    _tokens: float = field(default=0.0, repr=False)
    _last_refill: float = field(default=0.0, repr=False)

    def __post_init__(self):
        if self.burst_bytes == 0 and self.rate_kbps > 0:
            # Default burst = 2x the per-packet budget at 20ms ptime
            self.burst_bytes = int((self.rate_kbps * 1000 / 8) * 0.040)
        self._tokens = float(self.burst_bytes)
        self._last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._last_refill = now
        rate_bytes_per_sec = (self.rate_kbps * 1000.0) / 8.0
        self._tokens = min(
            float(self.burst_bytes),
            self._tokens + elapsed * rate_bytes_per_sec,
        )

    def should_throttle(self, packet_size: int) -> bool:
        """Returns True if the packet should be delayed/dropped due to bandwidth limit."""
        if not self.enabled or self.rate_kbps <= 0:
            return False
        self._refill()
        if self._tokens >= packet_size:
            self._tokens -= packet_size
            return False
        return True

    def delay_for_packet(self, packet_size: int) -> float:
        """Returns seconds to delay to stay within bandwidth. 0 if no throttle needed."""
        if not self.enabled or self.rate_kbps <= 0:
            return 0.0
        self._refill()
        if self._tokens >= packet_size:
            self._tokens -= packet_size
            return 0.0
        deficit = packet_size - self._tokens
        rate_bytes_per_sec = (self.rate_kbps * 1000.0) / 8.0
        if rate_bytes_per_sec <= 0:
            return 0.0
        wait = deficit / rate_bytes_per_sec
        self._tokens = 0
        return wait

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "rate_kbps": self.rate_kbps,
            "burst_bytes": self.burst_bytes,
        }


# ─── Impairment Profile ──────────────────────────────────────────────────────

@dataclass
class ImpairmentProfile:
    """Complete network impairment configuration."""
    name: str = "custom"
    description: str = ""

    # Loss
    loss_model: LossModel = LossModel.NONE
    random_loss: RandomLossConfig = field(default_factory=RandomLossConfig)
    burst_loss: BurstLossConfig = field(default_factory=BurstLossConfig)
    gilbert_loss: GilbertElliottConfig = field(default_factory=GilbertElliottConfig)

    # Jitter
    jitter: JitterConfig = field(default_factory=JitterConfig)

    # Delay
    delay: DelayConfig = field(default_factory=DelayConfig)

    # Bandwidth
    bandwidth: BandwidthConfig = field(default_factory=BandwidthConfig)

    # Reorder
    reorder_rate: float = 0.0  # probability of out-of-order delivery

    # Duplication
    duplicate_rate: float = 0.0  # probability of duplicate packet

    def should_drop(self) -> bool:
        if self.loss_model == LossModel.RANDOM:
            return self.random_loss.should_drop()
        elif self.loss_model == LossModel.BURST:
            return self.burst_loss.should_drop()
        elif self.loss_model == LossModel.GILBERT_ELLIOTT:
            return self.gilbert_loss.should_drop()
        return False

    def compute_total_delay_ms(self) -> float:
        """Returns total impairment delay in milliseconds."""
        return self.delay.compute_delay() + self.jitter.compute_jitter()

    def should_duplicate(self) -> bool:
        return self.duplicate_rate > 0 and random.random() < self.duplicate_rate

    def should_reorder(self) -> bool:
        return self.reorder_rate > 0 and random.random() < self.reorder_rate

    def to_dict(self) -> dict:
        loss_config: dict[str, Any] = {"model": self.loss_model.value}
        if self.loss_model == LossModel.RANDOM:
            loss_config.update(self.random_loss.to_dict())
        elif self.loss_model == LossModel.BURST:
            loss_config.update(self.burst_loss.to_dict())
        elif self.loss_model == LossModel.GILBERT_ELLIOTT:
            loss_config.update(self.gilbert_loss.to_dict())

        return {
            "name": self.name,
            "description": self.description,
            "loss": loss_config,
            "jitter": self.jitter.to_dict(),
            "delay": self.delay.to_dict(),
            "bandwidth": self.bandwidth.to_dict(),
            "reorder_rate": self.reorder_rate,
            "duplicate_rate": self.duplicate_rate,
        }


# ─── Built-in Profiles ───────────────────────────────────────────────────────

BUILTIN_PROFILES: dict[str, ImpairmentProfile] = {
    "perfect": ImpairmentProfile(
        name="perfect",
        description="No impairment - ideal network conditions",
    ),

    "lan": ImpairmentProfile(
        name="lan",
        description="Local area network - minimal impairment",
        delay=DelayConfig(model=DelayModel.FIXED, fixed_ms=1.0),
        jitter=JitterConfig(model=JitterModel.UNIFORM, min_ms=0.0, max_ms=0.5),
    ),

    "wifi": ImpairmentProfile(
        name="wifi",
        description="Typical WiFi connection with occasional loss",
        loss_model=LossModel.RANDOM,
        random_loss=RandomLossConfig(rate=0.02),
        delay=DelayConfig(model=DelayModel.VARIABLE, min_ms=5.0, max_ms=30.0),
        jitter=JitterConfig(model=JitterModel.NORMAL, mean_ms=5.0, stddev_ms=3.0),
        bandwidth=BandwidthConfig(enabled=True, rate_kbps=5000),
    ),

    "4g": ImpairmentProfile(
        name="4g",
        description="4G/LTE mobile connection",
        loss_model=LossModel.RANDOM,
        random_loss=RandomLossConfig(rate=0.01),
        delay=DelayConfig(model=DelayModel.VARIABLE, min_ms=20.0, max_ms=60.0),
        jitter=JitterConfig(model=JitterModel.NORMAL, mean_ms=8.0, stddev_ms=5.0),
        bandwidth=BandwidthConfig(enabled=True, rate_kbps=10000),
    ),

    "3g": ImpairmentProfile(
        name="3g",
        description="3G mobile connection - high latency and jitter",
        loss_model=LossModel.GILBERT_ELLIOTT,
        gilbert_loss=GilbertElliottConfig(p_g=0.02, p_b=0.15, p_gb=0.05, p_bg=0.20),
        delay=DelayConfig(model=DelayModel.VARIABLE, min_ms=50.0, max_ms=200.0),
        jitter=JitterConfig(model=JitterModel.NORMAL, mean_ms=20.0, stddev_ms=15.0),
        bandwidth=BandwidthConfig(enabled=True, rate_kbps=1500),
    ),

    "satellite": ImpairmentProfile(
        name="satellite",
        description="Satellite link - very high latency",
        loss_model=LossModel.RANDOM,
        random_loss=RandomLossConfig(rate=0.03),
        delay=DelayConfig(model=DelayModel.VARIABLE, min_ms=500.0, max_ms=700.0),
        jitter=JitterConfig(model=JitterModel.NORMAL, mean_ms=15.0, stddev_ms=10.0),
        bandwidth=BandwidthConfig(enabled=True, rate_kbps=2000),
    ),

    "congested": ImpairmentProfile(
        name="congested",
        description="Congested network - heavy loss and high jitter",
        loss_model=LossModel.GILBERT_ELLIOTT,
        gilbert_loss=GilbertElliottConfig(p_g=0.05, p_b=0.40, p_gb=0.10, p_bg=0.15),
        delay=DelayConfig(model=DelayModel.VARIABLE, min_ms=30.0, max_ms=150.0),
        jitter=JitterConfig(model=JitterModel.NORMAL, mean_ms=30.0, stddev_ms=20.0),
        bandwidth=BandwidthConfig(enabled=True, rate_kbps=500),
        reorder_rate=0.03,
        duplicate_rate=0.01,
    ),

    "terrible": ImpairmentProfile(
        name="terrible",
        description="Terrible network - extreme impairment for stress testing",
        loss_model=LossModel.GILBERT_ELLIOTT,
        gilbert_loss=GilbertElliottConfig(p_g=0.10, p_b=0.60, p_gb=0.15, p_bg=0.10),
        delay=DelayConfig(model=DelayModel.VARIABLE, min_ms=100.0, max_ms=500.0),
        jitter=JitterConfig(model=JitterModel.NORMAL, mean_ms=50.0, stddev_ms=40.0),
        bandwidth=BandwidthConfig(enabled=True, rate_kbps=200),
        reorder_rate=0.08,
        duplicate_rate=0.05,
    ),
}


# ─── Impairment Statistics ────────────────────────────────────────────────────

@dataclass
class ImpairmentStats:
    """Tracks what the simulator has actually done."""
    packets_processed: int = 0
    packets_dropped: int = 0
    packets_delayed: int = 0
    packets_duplicated: int = 0
    packets_reordered: int = 0
    packets_throttled: int = 0
    total_added_delay_ms: float = 0.0
    started_at: Optional[float] = None

    @property
    def drop_rate(self) -> float:
        if self.packets_processed == 0:
            return 0.0
        return (self.packets_dropped / self.packets_processed) * 100.0

    @property
    def avg_added_delay_ms(self) -> float:
        delivered = self.packets_processed - self.packets_dropped
        if delivered == 0:
            return 0.0
        return self.total_added_delay_ms / delivered

    @property
    def uptime_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        return time.monotonic() - self.started_at

    def reset(self) -> None:
        self.packets_processed = 0
        self.packets_dropped = 0
        self.packets_delayed = 0
        self.packets_duplicated = 0
        self.packets_reordered = 0
        self.packets_throttled = 0
        self.total_added_delay_ms = 0.0
        self.started_at = time.monotonic()

    def to_dict(self) -> dict:
        return {
            "packets_processed": self.packets_processed,
            "packets_dropped": self.packets_dropped,
            "packets_delayed": self.packets_delayed,
            "packets_duplicated": self.packets_duplicated,
            "packets_reordered": self.packets_reordered,
            "packets_throttled": self.packets_throttled,
            "total_added_delay_ms": round(self.total_added_delay_ms, 2),
            "avg_added_delay_ms": round(self.avg_added_delay_ms, 2),
            "drop_rate_pct": round(self.drop_rate, 2),
            "uptime_seconds": round(self.uptime_seconds, 1),
        }


# ─── Packet Decision ─────────────────────────────────────────────────────────

class PacketAction(Enum):
    SEND = "send"
    DROP = "drop"
    DELAY = "delay"
    DUPLICATE = "duplicate"


@dataclass
class PacketDecision:
    """The simulator's decision for a single packet."""
    action: PacketAction = PacketAction.SEND
    delay_ms: float = 0.0
    duplicate: bool = False

    def to_dict(self) -> dict:
        return {
            "action": self.action.value,
            "delay_ms": round(self.delay_ms, 3),
            "duplicate": self.duplicate,
        }


# ─── Network Impairment Simulator ────────────────────────────────────────────

class NetworkSimulator:
    """
    Applies network impairments to packet streams.
    Thread-safe, supports real-time profile switching.
    """

    def __init__(self, profile: Optional[ImpairmentProfile] = None):
        self._profile = profile or BUILTIN_PROFILES["perfect"]
        self._lock = threading.Lock()
        self._stats = ImpairmentStats()
        self._stats.started_at = time.monotonic()
        self._enabled = True
        self._listeners: list[Callable[[PacketDecision, ImpairmentStats], Any]] = []

        logger.info("Network simulator initialized with profile: %s", self._profile.name)

    # ─── Profile Management ───────────────────────────────────────────────

    @property
    def profile(self) -> ImpairmentProfile:
        with self._lock:
            return self._profile

    def set_profile(self, profile: ImpairmentProfile) -> None:
        with self._lock:
            self._profile = profile
            self._stats.reset()
        logger.info("Network profile changed to: %s", profile.name)

    def load_builtin_profile(self, name: str) -> bool:
        profile = BUILTIN_PROFILES.get(name)
        if profile is None:
            logger.warning("Unknown builtin profile: %s", name)
            return False
        self.set_profile(profile)
        return True

    @staticmethod
    def list_profiles() -> list[dict]:
        return [p.to_dict() for p in BUILTIN_PROFILES.values()]

    @staticmethod
    def get_profile(name: str) -> Optional[ImpairmentProfile]:
        return BUILTIN_PROFILES.get(name)

    # ─── Real-time Parameter Adjustment ───────────────────────────────────

    def set_loss_rate(self, rate: float) -> None:
        """Adjust random loss rate on the fly (0.0 - 1.0)."""
        with self._lock:
            self._profile.loss_model = LossModel.RANDOM
            self._profile.random_loss.rate = max(0.0, min(1.0, rate))
        logger.debug("Loss rate set to %.2f", rate)

    def set_delay(self, fixed_ms: float) -> None:
        """Set fixed delay in ms."""
        with self._lock:
            self._profile.delay.model = DelayModel.FIXED
            self._profile.delay.fixed_ms = max(0.0, fixed_ms)
        logger.debug("Delay set to %.1fms", fixed_ms)

    def set_jitter(self, mean_ms: float, stddev_ms: float) -> None:
        """Set normal-distribution jitter."""
        with self._lock:
            self._profile.jitter.model = JitterModel.NORMAL
            self._profile.jitter.mean_ms = max(0.0, mean_ms)
            self._profile.jitter.stddev_ms = max(0.0, stddev_ms)
        logger.debug("Jitter set to N(%.1f, %.1f)ms", mean_ms, stddev_ms)

    def set_bandwidth(self, rate_kbps: float) -> None:
        """Set bandwidth limit in kbps (0 = unlimited)."""
        with self._lock:
            if rate_kbps > 0:
                self._profile.bandwidth.enabled = True
                self._profile.bandwidth.rate_kbps = rate_kbps
            else:
                self._profile.bandwidth.enabled = False
        logger.debug("Bandwidth set to %.0f kbps", rate_kbps)

    # ─── Enable / Disable ─────────────────────────────────────────────────

    def enable(self) -> None:
        self._enabled = True
        logger.info("Network simulator enabled")

    def disable(self) -> None:
        self._enabled = False
        logger.info("Network simulator disabled (passthrough)")

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ─── Core Processing ──────────────────────────────────────────────────

    def process_packet(self, packet_data: bytes) -> PacketDecision:
        """
        Decide what to do with a packet.
        Returns a PacketDecision the caller should apply.
        """
        decision = PacketDecision()

        if not self._enabled:
            return decision

        with self._lock:
            profile = self._profile
            self._stats.packets_processed += 1

            # 1. Loss check
            if profile.should_drop():
                decision.action = PacketAction.DROP
                self._stats.packets_dropped += 1
                self._notify(decision)
                return decision

            # 2. Bandwidth throttle
            bw_delay = profile.bandwidth.delay_for_packet(len(packet_data))
            if bw_delay > 0:
                self._stats.packets_throttled += 1

            # 3. Delay + jitter
            impairment_delay = profile.compute_total_delay_ms()
            total_delay = impairment_delay + (bw_delay * 1000.0)

            if total_delay > 0:
                decision.action = PacketAction.DELAY
                decision.delay_ms = total_delay
                self._stats.packets_delayed += 1
                self._stats.total_added_delay_ms += total_delay

            # 4. Duplication
            if profile.should_duplicate():
                decision.duplicate = True
                self._stats.packets_duplicated += 1

            # 5. Reorder (recorded for stats; actual reorder is caller's job)
            if profile.should_reorder():
                self._stats.packets_reordered += 1

        self._notify(decision)
        return decision

    def apply_delay(self, decision: PacketDecision) -> None:
        """Sleep for the decided delay. Call from the sending thread."""
        if decision.delay_ms > 0:
            time.sleep(decision.delay_ms / 1000.0)

    # ─── Stats ────────────────────────────────────────────────────────────

    @property
    def stats(self) -> ImpairmentStats:
        with self._lock:
            return self._stats

    def reset_stats(self) -> None:
        with self._lock:
            self._stats.reset()

    def add_listener(self, callback: Callable[[PacketDecision, ImpairmentStats], Any]) -> None:
        self._listeners.append(callback)

    def _notify(self, decision: PacketDecision) -> None:
        for listener in self._listeners:
            try:
                listener(decision, self._stats)
            except Exception:
                pass

    # ─── Serialization ────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "enabled": self._enabled,
                "profile": self._profile.to_dict(),
                "stats": self._stats.to_dict(),
            }
