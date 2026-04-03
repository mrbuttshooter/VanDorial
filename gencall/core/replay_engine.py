"""
GenCall - Production Traffic Replay Engine

Captures real production SIP traffic patterns and replays them:
  - Record call patterns from live CDRs (timing, distribution, codecs)
  - Build a statistical model of the traffic
  - Replay the exact pattern against a test target
  - Scale up/down while maintaining the same distribution
  - A/B comparison: replay same pattern against two targets

This lets you take REAL production traffic and replay it in a lab.
No more guessing what "realistic traffic" looks like.
"""

import time
import random
import math
import json
import logging
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("gencall.replay_engine")


@dataclass
class CallPattern:
    """A single call event in a traffic pattern."""
    offset_sec: float        # seconds from pattern start
    caller: str = ""
    callee: str = ""
    duration_sec: float = 0  # expected call duration
    codec: str = "PCMA"
    scenario: str = "basic_call"

    def to_dict(self) -> dict:
        return {
            "offset_sec": round(self.offset_sec, 3),
            "caller": self.caller,
            "callee": self.callee,
            "duration_sec": round(self.duration_sec, 1),
            "codec": self.codec,
            "scenario": self.scenario,
        }


@dataclass
class TrafficModel:
    """
    Statistical model of traffic learned from production CDRs.
    Can generate synthetic traffic that matches the real distribution.
    """
    name: str = "unnamed"
    source: str = ""
    recorded_at: str = ""
    total_duration_sec: float = 0

    # Distributions learned from data
    calls_per_minute: list[float] = field(default_factory=list)  # 1440 entries (24h * 60min)
    call_duration_distribution: list[float] = field(default_factory=list)  # sampled durations
    codec_distribution: dict[str, float] = field(default_factory=dict)  # codec -> probability
    caller_prefix_distribution: dict[str, float] = field(default_factory=dict)
    callee_prefix_distribution: dict[str, float] = field(default_factory=dict)

    # Raw patterns for exact replay
    patterns: list[CallPattern] = field(default_factory=list)

    @property
    def total_calls(self) -> int:
        return len(self.patterns)

    @property
    def peak_cpm(self) -> float:
        return max(self.calls_per_minute) if self.calls_per_minute else 0

    @property
    def avg_duration(self) -> float:
        if not self.call_duration_distribution:
            return 0
        return sum(self.call_duration_distribution) / len(self.call_duration_distribution)

    def save(self, filepath: str):
        """Save the traffic model to JSON."""
        data = {
            "name": self.name,
            "source": self.source,
            "recorded_at": self.recorded_at,
            "total_duration_sec": self.total_duration_sec,
            "total_calls": self.total_calls,
            "peak_cpm": self.peak_cpm,
            "avg_duration_sec": self.avg_duration,
            "calls_per_minute": self.calls_per_minute,
            "call_duration_distribution": self.call_duration_distribution[:1000],
            "codec_distribution": self.codec_distribution,
            "caller_prefix_distribution": self.caller_prefix_distribution,
            "callee_prefix_distribution": self.callee_prefix_distribution,
            "patterns": [p.to_dict() for p in self.patterns[:50000]],
        }
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
        logger.info("Traffic model saved: %s (%d calls)", filepath, self.total_calls)

    @classmethod
    def load(cls, filepath: str) -> "TrafficModel":
        """Load a traffic model from JSON."""
        with open(filepath, "r") as f:
            data = json.load(f)

        model = cls(
            name=data.get("name", ""),
            source=data.get("source", ""),
            recorded_at=data.get("recorded_at", ""),
            total_duration_sec=data.get("total_duration_sec", 0),
            calls_per_minute=data.get("calls_per_minute", []),
            call_duration_distribution=data.get("call_duration_distribution", []),
            codec_distribution=data.get("codec_distribution", {}),
            caller_prefix_distribution=data.get("caller_prefix_distribution", {}),
            callee_prefix_distribution=data.get("callee_prefix_distribution", {}),
        )

        for p in data.get("patterns", []):
            model.patterns.append(CallPattern(**p))

        logger.info("Traffic model loaded: %s (%d calls)", filepath, model.total_calls)
        return model

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "source": self.source,
            "total_calls": self.total_calls,
            "total_duration_sec": self.total_duration_sec,
            "peak_cpm": round(self.peak_cpm, 1),
            "avg_duration_sec": round(self.avg_duration, 1),
            "codec_distribution": self.codec_distribution,
        }


class TrafficRecorder:
    """
    Records traffic patterns from CDR data to build a TrafficModel.
    """

    def __init__(self, name: str = "recording"):
        self.name = name
        self._cdrs: list[dict] = []
        self._start_time: float = 0

    def add_cdr(self, cdr: dict):
        """Add a CDR record to the recording."""
        if not self._start_time:
            self._start_time = cdr.get("start_time", time.time())
        self._cdrs.append(cdr)

    def build_model(self) -> TrafficModel:
        """Build a TrafficModel from recorded CDRs."""
        if not self._cdrs:
            return TrafficModel(name=self.name)

        model = TrafficModel(
            name=self.name,
            source="live_recording",
            recorded_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )

        # Sort by start time
        sorted_cdrs = sorted(self._cdrs, key=lambda c: c.get("start_time", 0))
        first_time = sorted_cdrs[0].get("start_time", 0)
        last_time = sorted_cdrs[-1].get("start_time", 0)
        model.total_duration_sec = last_time - first_time

        # Build call patterns
        for cdr in sorted_cdrs:
            offset = cdr.get("start_time", 0) - first_time
            model.patterns.append(CallPattern(
                offset_sec=offset,
                caller=cdr.get("caller", ""),
                callee=cdr.get("callee", ""),
                duration_sec=cdr.get("duration", 0),
                codec=cdr.get("codec", "PCMA"),
                scenario=cdr.get("scenario", "basic_call"),
            ))

        # Build distributions
        model.call_duration_distribution = [
            cdr.get("duration", 0) for cdr in sorted_cdrs
        ]

        # Codec distribution
        codec_counts = Counter(cdr.get("codec", "PCMA") for cdr in sorted_cdrs)
        total = sum(codec_counts.values())
        model.codec_distribution = {k: v / total for k, v in codec_counts.items()}

        # Calls per minute (24h distribution)
        import datetime
        minute_counts = [0.0] * 1440
        for cdr in sorted_cdrs:
            ts = cdr.get("start_time", 0)
            dt = datetime.datetime.fromtimestamp(ts)
            minute_idx = dt.hour * 60 + dt.minute
            minute_counts[minute_idx] += 1
        model.calls_per_minute = minute_counts

        # Caller/callee prefix distribution
        caller_prefixes = Counter()
        callee_prefixes = Counter()
        for cdr in sorted_cdrs:
            caller = cdr.get("caller", "")
            callee = cdr.get("callee", "")
            if len(caller) >= 3:
                caller_prefixes[caller[:3]] += 1
            if len(callee) >= 3:
                callee_prefixes[callee[:3]] += 1

        total_callers = sum(caller_prefixes.values()) or 1
        total_callees = sum(callee_prefixes.values()) or 1
        model.caller_prefix_distribution = {k: v / total_callers for k, v in caller_prefixes.most_common(50)}
        model.callee_prefix_distribution = {k: v / total_callees for k, v in callee_prefixes.most_common(50)}

        logger.info("Traffic model built: %d calls, %.1fs duration, peak %.1f cpm",
                     model.total_calls, model.total_duration_sec, model.peak_cpm)

        return model


class TrafficReplayer:
    """
    Replays a TrafficModel against a target.
    Can replay exact patterns or generate synthetic traffic from distributions.
    """

    def __init__(self, model: TrafficModel, ctx=None):
        self.model = model
        self.ctx = ctx
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._calls_placed = 0
        self._speed = 1.0
        self._scale = 1.0
        self._progress_callbacks: list = []

    def set_speed(self, multiplier: float):
        """Set replay speed. 2.0 = twice as fast, 0.5 = half speed."""
        self._speed = max(0.1, min(100.0, multiplier))

    def set_scale(self, multiplier: float):
        """Scale the call volume. 2.0 = double the calls."""
        self._scale = max(0.1, min(100.0, multiplier))

    def on_progress(self, callback):
        self._progress_callbacks.append(callback)

    def replay_exact(self):
        """Replay the exact recorded call pattern."""
        if not self.model.patterns:
            logger.warning("No patterns to replay")
            return

        self._running = True
        self._calls_placed = 0
        start_time = time.time()

        logger.info("Starting exact replay: %d calls, speed=%.1fx, scale=%.1fx",
                     self.model.total_calls, self._speed, self._scale)

        for pattern in self.model.patterns:
            if not self._running:
                break

            # Calculate when this call should fire
            target_time = start_time + (pattern.offset_sec / self._speed)
            now = time.time()

            if target_time > now:
                sleep_time = target_time - now
                time.sleep(sleep_time)

            if not self._running:
                break

            # Scale: randomly skip calls if scale < 1, duplicate if > 1
            num_calls = max(1, int(self._scale))
            if self._scale < 1.0 and random.random() > self._scale:
                continue

            for _ in range(num_calls):
                self._place_call(pattern)

        self._running = False
        elapsed = time.time() - start_time
        logger.info("Replay complete: %d calls in %.1fs", self._calls_placed, elapsed)

    def replay_synthetic(self, duration_sec: float = 3600):
        """
        Generate synthetic traffic matching the model's statistical distribution.
        Runs for the specified duration.
        """
        self._running = True
        self._calls_placed = 0
        start_time = time.time()

        logger.info("Starting synthetic replay: duration=%ds, scale=%.1fx",
                     duration_sec, self._scale)

        import datetime

        while self._running and (time.time() - start_time) < duration_sec:
            now_dt = datetime.datetime.now()
            minute_idx = now_dt.hour * 60 + now_dt.minute

            # Get expected calls per minute from the model
            if minute_idx < len(self.model.calls_per_minute):
                target_cpm = self.model.calls_per_minute[minute_idx] * self._scale
            else:
                target_cpm = 1.0 * self._scale

            if target_cpm <= 0:
                time.sleep(1)
                continue

            # Inter-call delay for this rate
            delay = 60.0 / target_cpm / self._speed

            # Generate a synthetic call
            pattern = self._generate_synthetic_call()
            self._place_call(pattern)

            time.sleep(max(0.01, delay))

        self._running = False
        elapsed = time.time() - start_time
        logger.info("Synthetic replay complete: %d calls in %.1fs", self._calls_placed, elapsed)

    def _generate_synthetic_call(self) -> CallPattern:
        """Generate a single call from the model's distributions."""
        # Duration from distribution
        if self.model.call_duration_distribution:
            duration = random.choice(self.model.call_duration_distribution)
        else:
            duration = random.uniform(30, 300)

        # Codec from distribution
        codec = "PCMA"
        if self.model.codec_distribution:
            codecs = list(self.model.codec_distribution.keys())
            weights = list(self.model.codec_distribution.values())
            codec = random.choices(codecs, weights=weights, k=1)[0]

        # Generate phone numbers from prefix distribution
        caller = self._random_number_from_distribution(self.model.caller_prefix_distribution)
        callee = self._random_number_from_distribution(self.model.callee_prefix_distribution)

        return CallPattern(
            offset_sec=0,
            caller=caller,
            callee=callee,
            duration_sec=duration,
            codec=codec,
        )

    @staticmethod
    def _random_number_from_distribution(prefix_dist: dict[str, float]) -> str:
        """Generate a random phone number using the prefix distribution."""
        if prefix_dist:
            prefixes = list(prefix_dist.keys())
            weights = list(prefix_dist.values())
            prefix = random.choices(prefixes, weights=weights, k=1)[0]
        else:
            prefix = str(random.randint(100, 999))

        # Generate remaining digits
        remaining = 10 - len(prefix)
        suffix = "".join(str(random.randint(0, 9)) for _ in range(max(0, remaining)))
        return prefix + suffix

    def _place_call(self, pattern: CallPattern):
        """Place a single call (or queue it for the engine)."""
        self._calls_placed += 1

        if self.ctx:
            try:
                self.ctx.place_call(
                    caller=pattern.caller,
                    callee=pattern.callee,
                    duration=pattern.duration_sec,
                    codec=pattern.codec,
                    scenario=pattern.scenario,
                )
            except Exception as e:
                logger.debug("Call placement failed: %s", e)

        # Progress callback
        for cb in self._progress_callbacks:
            try:
                cb({
                    "calls_placed": self._calls_placed,
                    "current_call": pattern.to_dict(),
                    "running": self._running,
                })
            except Exception:
                pass

    def stop(self):
        self._running = False

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "calls_placed": self._calls_placed,
            "speed": self._speed,
            "scale": self._scale,
            "model": self.model.to_dict(),
        }


@dataclass
class ABTestResult:
    """Result of replaying the same traffic against two targets."""
    target_a: str
    target_b: str
    model_name: str
    calls_a: int = 0
    calls_b: int = 0
    success_rate_a: float = 0.0
    success_rate_b: float = 0.0
    avg_pdd_a_ms: float = 0.0
    avg_pdd_b_ms: float = 0.0
    avg_mos_a: float = 0.0
    avg_mos_b: float = 0.0

    @property
    def winner(self) -> str:
        score_a = self.success_rate_a + self.avg_mos_a * 20 - self.avg_pdd_a_ms * 0.01
        score_b = self.success_rate_b + self.avg_mos_b * 20 - self.avg_pdd_b_ms * 0.01
        if score_a > score_b:
            return "A"
        elif score_b > score_a:
            return "B"
        return "TIE"

    def report(self) -> str:
        w = self.winner
        return "\n".join([
            "",
            "=" * 60,
            "  A/B TRAFFIC REPLAY COMPARISON",
            "=" * 60,
            f"  Model: {self.model_name}",
            "",
            f"  {'Metric':<20} {'Target A':>15} {'Target B':>15}",
            f"  {'─' * 50}",
            f"  {'Target':<20} {self.target_a:>15} {self.target_b:>15}",
            f"  {'Calls':<20} {self.calls_a:>15} {self.calls_b:>15}",
            f"  {'Success Rate':<20} {self.success_rate_a:>14.1f}% {self.success_rate_b:>14.1f}%",
            f"  {'Avg PDD':<20} {self.avg_pdd_a_ms:>13.0f}ms {self.avg_pdd_b_ms:>13.0f}ms",
            f"  {'Avg MOS':<20} {self.avg_mos_a:>15.2f} {self.avg_mos_b:>15.2f}",
            "",
            f"  Winner: Target {w}" if w != "TIE" else "  Result: TIE",
            "=" * 60,
        ])

    def to_dict(self) -> dict:
        return {
            "target_a": self.target_a,
            "target_b": self.target_b,
            "model_name": self.model_name,
            "calls_a": self.calls_a,
            "calls_b": self.calls_b,
            "success_rate_a": self.success_rate_a,
            "success_rate_b": self.success_rate_b,
            "avg_pdd_a_ms": self.avg_pdd_a_ms,
            "avg_pdd_b_ms": self.avg_pdd_b_ms,
            "avg_mos_a": self.avg_mos_a,
            "avg_mos_b": self.avg_mos_b,
            "winner": self.winner,
        }
