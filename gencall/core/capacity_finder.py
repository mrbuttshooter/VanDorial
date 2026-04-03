"""
GenCall Capacity Finder - Automated CPS benchmarking via binary search.

Discovers the maximum sustained calls-per-second (CPS) a SIP target can
handle by ramping load, detecting the failure threshold, and then binary
searching for the exact tipping point.  Each step holds for a configurable
dwell time so transient spikes do not skew results.

Supports multiple scenarios, progress callbacks, and generates a detailed
capacity report with breakpoint analysis.
"""

from __future__ import annotations

import datetime
import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger("gencall.capacity_finder")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class TestPhase(Enum):
    IDLE = "idle"
    RAMP_UP = "ramp_up"
    BINARY_SEARCH = "binary_search"
    VERIFICATION = "verification"
    COMPLETED = "completed"
    ABORTED = "aborted"
    ERROR = "error"


class FailureMode(Enum):
    NONE = "none"
    LOW_SUCCESS_RATE = "low_success_rate"
    HIGH_RESPONSE_TIME = "high_response_time"
    EXCESSIVE_RETRANSMISSIONS = "excessive_retransmissions"
    ENGINE_ERROR = "engine_error"
    TIMEOUT = "timeout"


@dataclass
class SuccessCriteria:
    """Thresholds that define a "healthy" CPS level."""
    min_success_rate_pct: float = 99.0
    max_response_time_ms: float = 500.0
    max_retransmission_rate_pct: float = 5.0

    def to_dict(self) -> dict:
        return {
            "min_success_rate_pct": self.min_success_rate_pct,
            "max_response_time_ms": self.max_response_time_ms,
            "max_retransmission_rate_pct": self.max_retransmission_rate_pct,
        }


@dataclass
class StepResult:
    """Result from a single CPS test step."""
    step_number: int = 0
    cps: float = 0.0
    phase: TestPhase = TestPhase.IDLE
    dwell_seconds: float = 0.0
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    retransmissions: int = 0
    avg_response_time_ms: float = 0.0
    success_rate_pct: float = 0.0
    retransmission_rate_pct: float = 0.0
    passed: bool = False
    failure_mode: FailureMode = FailureMode.NONE
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "step_number": self.step_number,
            "cps": round(self.cps, 2),
            "phase": self.phase.value,
            "dwell_seconds": round(self.dwell_seconds, 1),
            "total_calls": self.total_calls,
            "successful_calls": self.successful_calls,
            "failed_calls": self.failed_calls,
            "retransmissions": self.retransmissions,
            "avg_response_time_ms": round(self.avg_response_time_ms, 2),
            "success_rate_pct": round(self.success_rate_pct, 2),
            "retransmission_rate_pct": round(self.retransmission_rate_pct, 2),
            "passed": self.passed,
            "failure_mode": self.failure_mode.value,
            "timestamp": round(self.timestamp, 3),
        }


@dataclass
class CapacityReport:
    """Final output of a capacity discovery run."""
    report_id: str = ""
    scenario_name: str = ""
    target: str = ""
    started_at: Optional[datetime.datetime] = None
    completed_at: Optional[datetime.datetime] = None
    duration_seconds: float = 0.0
    phase: TestPhase = TestPhase.IDLE
    criteria: SuccessCriteria = field(default_factory=SuccessCriteria)

    max_stable_cps: float = 0.0
    breaking_point_cps: float = 0.0
    failure_mode: FailureMode = FailureMode.NONE

    steps: list[StepResult] = field(default_factory=list)

    # Config echo
    initial_cps: float = 0.0
    max_cps: float = 0.0
    step_size: float = 0.0
    dwell_time: float = 0.0
    precision: float = 0.0

    error_message: str = ""

    def __post_init__(self) -> None:
        if not self.report_id:
            self.report_id = uuid.uuid4().hex[:12]

    @property
    def summary(self) -> str:
        if self.phase == TestPhase.COMPLETED:
            return (
                f"Capacity test for {self.scenario_name} on {self.target}: "
                f"max stable CPS = {self.max_stable_cps:.1f}, "
                f"breaking point = {self.breaking_point_cps:.1f} "
                f"({self.failure_mode.value})"
            )
        return f"Capacity test {self.phase.value}: {self.error_message or 'in progress'}"

    def to_dict(self) -> dict:
        return {
            "report_id": self.report_id,
            "scenario_name": self.scenario_name,
            "target": self.target,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_seconds": round(self.duration_seconds, 2),
            "phase": self.phase.value,
            "criteria": self.criteria.to_dict(),
            "max_stable_cps": round(self.max_stable_cps, 2),
            "breaking_point_cps": round(self.breaking_point_cps, 2),
            "failure_mode": self.failure_mode.value,
            "steps": [s.to_dict() for s in self.steps],
            "config": {
                "initial_cps": self.initial_cps,
                "max_cps": self.max_cps,
                "step_size": self.step_size,
                "dwell_time": self.dwell_time,
                "precision": self.precision,
            },
            "summary": self.summary,
            "error_message": self.error_message,
        }


@dataclass
class ScenarioComparison:
    """Side-by-side comparison of capacity results for multiple scenarios."""
    comparison_id: str = ""
    reports: list[CapacityReport] = field(default_factory=list)
    best_scenario: str = ""
    worst_scenario: str = ""
    created_at: Optional[datetime.datetime] = None

    def __post_init__(self) -> None:
        if not self.comparison_id:
            self.comparison_id = uuid.uuid4().hex[:12]

    def to_dict(self) -> dict:
        return {
            "comparison_id": self.comparison_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "best_scenario": self.best_scenario,
            "worst_scenario": self.worst_scenario,
            "reports": [r.to_dict() for r in self.reports],
            "ranking": [
                {
                    "scenario": r.scenario_name,
                    "max_stable_cps": round(r.max_stable_cps, 2),
                    "breaking_point_cps": round(r.breaking_point_cps, 2),
                    "failure_mode": r.failure_mode.value,
                }
                for r in sorted(self.reports, key=lambda x: x.max_stable_cps, reverse=True)
            ],
        }


# ---------------------------------------------------------------------------
# Metrics collector interface
# ---------------------------------------------------------------------------

class MetricsCollector:
    """
    Adapter that reads live stats from the SIPp engine.

    Subclass or replace with a real implementation that reads from
    ``SIPpEngine`` / ``StatsEngine``.  The capacity finder calls
    ``collect()`` after each dwell period to get the numbers for
    the period.
    """

    def reset(self) -> None:
        """Reset counters for a new measurement window."""

    def collect(self) -> dict[str, Any]:
        """
        Return metrics for the elapsed window.

        Expected keys:
            total_calls, successful_calls, failed_calls,
            retransmissions, avg_response_time_ms
        """
        return {
            "total_calls": 0,
            "successful_calls": 0,
            "failed_calls": 0,
            "retransmissions": 0,
            "avg_response_time_ms": 0.0,
        }


class SIPpMetricsCollector(MetricsCollector):
    """Reads live metrics from a running SIPp engine instance."""

    def __init__(self, sipp_engine: Any, instance_id: str = "") -> None:
        self._engine = sipp_engine
        self._instance_id = instance_id
        self._baseline: dict[str, int] = {}

    def reset(self) -> None:
        inst = self._get_instance()
        if inst is None:
            self._baseline = {}
            return
        s = inst.stats
        self._baseline = {
            "total_calls": s.total_calls,
            "successful_calls": s.successful_calls,
            "failed_calls": s.failed_calls,
            "retransmissions": s.retransmissions,
        }

    def collect(self) -> dict[str, Any]:
        inst = self._get_instance()
        if inst is None:
            return {
                "total_calls": 0,
                "successful_calls": 0,
                "failed_calls": 0,
                "retransmissions": 0,
                "avg_response_time_ms": 0.0,
            }
        s = inst.stats
        return {
            "total_calls": s.total_calls - self._baseline.get("total_calls", 0),
            "successful_calls": s.successful_calls - self._baseline.get("successful_calls", 0),
            "failed_calls": s.failed_calls - self._baseline.get("failed_calls", 0),
            "retransmissions": s.retransmissions - self._baseline.get("retransmissions", 0),
            "avg_response_time_ms": s.avg_response_time_ms,
        }

    def _get_instance(self) -> Any:
        if self._instance_id:
            return self._engine.instances.get(self._instance_id)
        # Return the first running instance
        for inst in self._engine.instances.values():
            if hasattr(inst, "state") and inst.state.value == "running":
                return inst
        return None


# ---------------------------------------------------------------------------
# Rate controller interface
# ---------------------------------------------------------------------------

class RateController:
    """
    Adapter that adjusts the CPS on the traffic source.

    The default implementation calls ``sipp_engine.update_call_rate()``.
    Replace if your load source has a different API.
    """

    def set_rate(self, cps: float) -> bool:
        """Set the call rate and return True on success."""
        logger.info("RateController.set_rate(%.2f) [no-op base]", cps)
        return True


class SIPpRateController(RateController):
    """Adjusts the call rate on the first running SIPp instance."""

    def __init__(self, sipp_engine: Any, instance_id: str = "") -> None:
        self._engine = sipp_engine
        self._instance_id = instance_id

    def set_rate(self, cps: float) -> bool:
        target_id = self._instance_id
        if not target_id:
            for inst_id, inst in self._engine.instances.items():
                if hasattr(inst, "state") and inst.state.value == "running":
                    target_id = inst_id
                    break
        if not target_id:
            logger.warning("No running SIPp instance found for rate change")
            return False
        return self._engine.update_call_rate(target_id, cps)


# ---------------------------------------------------------------------------
# Progress callback type
# ---------------------------------------------------------------------------

ProgressCallback = Callable[[TestPhase, int, StepResult], None]


# ---------------------------------------------------------------------------
# Capacity Finder
# ---------------------------------------------------------------------------

class CapacityFinder:
    """
    Binary-search capacity / performance benchmarker.

    Algorithm
    ---------
    1. **Ramp-up**: start at ``initial_cps`` and increase by ``step_size``
       after each successful dwell period until a failure is observed.
    2. **Binary search**: the failure defines an upper bound and the last
       passing CPS is the lower bound.  The algorithm halves the search
       interval until the gap is smaller than ``precision``.
    3. **Verification**: optionally re-run the discovered max stable CPS
       for a longer dwell to confirm stability.

    Thread-safe: the finder runs in its own thread so the caller can
    poll progress or attach callbacks.
    """

    def __init__(
        self,
        rate_controller: RateController,
        metrics_collector: MetricsCollector,
        *,
        initial_cps: float = 1.0,
        max_cps: float = 1000.0,
        step_size: float = 10.0,
        dwell_time: float = 30.0,
        verification_dwell: float = 60.0,
        precision: float = 1.0,
        criteria: Optional[SuccessCriteria] = None,
        scenario_name: str = "default",
        target: str = "",
        progress_callback: Optional[ProgressCallback] = None,
    ) -> None:
        self._rate_ctrl = rate_controller
        self._metrics = metrics_collector

        self.initial_cps = max(0.1, initial_cps)
        self.max_cps = max_cps
        self.step_size = max(0.1, step_size)
        self.dwell_time = max(1.0, dwell_time)
        self.verification_dwell = max(1.0, verification_dwell)
        self.precision = max(0.1, precision)
        self.criteria = criteria or SuccessCriteria()
        self.scenario_name = scenario_name
        self.target = target

        self._callback = progress_callback
        self._report: Optional[CapacityReport] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._step_counter = 0

        logger.info(
            "CapacityFinder created: initial=%.1f, max=%.1f, step=%.1f, "
            "dwell=%.1fs, precision=%.1f, scenario=%s",
            self.initial_cps, self.max_cps, self.step_size,
            self.dwell_time, self.precision, self.scenario_name,
        )

    # -- public API --------------------------------------------------------

    @property
    def report(self) -> Optional[CapacityReport]:
        return self._report

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run(self) -> CapacityReport:
        """Run the capacity test synchronously.  Returns the final report."""
        return self._execute()

    def start(self) -> None:
        """Run the capacity test in a background thread."""
        if self.is_running:
            logger.warning("Capacity finder is already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._execute, daemon=True, name="capacity-finder",
        )
        self._thread.start()
        logger.info("Capacity finder started in background")

    def stop(self) -> None:
        """Request a graceful stop."""
        self._stop_event.set()
        logger.info("Capacity finder stop requested")

    def wait(self, timeout: Optional[float] = None) -> Optional[CapacityReport]:
        """Block until the background run completes."""
        if self._thread:
            self._thread.join(timeout=timeout)
        return self._report

    # -- internal ----------------------------------------------------------

    def _execute(self) -> CapacityReport:
        report = CapacityReport(
            scenario_name=self.scenario_name,
            target=self.target,
            started_at=datetime.datetime.utcnow(),
            criteria=self.criteria,
            initial_cps=self.initial_cps,
            max_cps=self.max_cps,
            step_size=self.step_size,
            dwell_time=self.dwell_time,
            precision=self.precision,
        )
        self._report = report
        self._step_counter = 0

        try:
            # Phase 1: ramp up
            lower, upper, ramp_failure = self._ramp_up(report)

            if self._stop_event.is_set():
                report.phase = TestPhase.ABORTED
                self._finalise(report)
                return report

            if ramp_failure is None:
                # Never failed - the target handled everything up to max_cps
                report.max_stable_cps = upper
                report.breaking_point_cps = 0.0
                report.failure_mode = FailureMode.NONE
                report.phase = TestPhase.COMPLETED
                self._finalise(report)
                return report

            report.breaking_point_cps = upper
            report.failure_mode = ramp_failure

            # Phase 2: binary search
            stable_cps = self._binary_search(report, lower, upper)

            if self._stop_event.is_set():
                report.phase = TestPhase.ABORTED
                self._finalise(report)
                return report

            # Phase 3: verification
            verified = self._verify(report, stable_cps)
            report.max_stable_cps = stable_cps if verified else max(0.0, stable_cps - self.precision)
            report.phase = TestPhase.COMPLETED

        except Exception as exc:
            report.phase = TestPhase.ERROR
            report.error_message = str(exc)
            logger.exception("Capacity finder error")

        self._finalise(report)
        return report

    # -- ramp up -----------------------------------------------------------

    def _ramp_up(self, report: CapacityReport) -> tuple[float, float, Optional[FailureMode]]:
        """
        Ramp CPS from ``initial_cps`` upward.

        Returns (last_passing_cps, failing_cps_or_max, failure_mode_or_None).
        """
        report.phase = TestPhase.RAMP_UP
        last_pass_cps = 0.0
        cps = self.initial_cps

        while cps <= self.max_cps:
            if self._stop_event.is_set():
                return last_pass_cps, cps, None

            step = self._run_step(cps, TestPhase.RAMP_UP, report)
            if step.passed:
                last_pass_cps = cps
                cps += self.step_size
            else:
                logger.info(
                    "Ramp-up failure at %.1f CPS (%s)", cps, step.failure_mode.value,
                )
                return last_pass_cps, cps, step.failure_mode

        # Reached max_cps without failure
        return last_pass_cps, self.max_cps, None

    # -- binary search -----------------------------------------------------

    def _binary_search(self, report: CapacityReport, low: float, high: float) -> float:
        """Narrow down the exact capacity between ``low`` (pass) and ``high`` (fail)."""
        report.phase = TestPhase.BINARY_SEARCH
        best = low

        while (high - low) > self.precision:
            if self._stop_event.is_set():
                return best

            mid = math.floor((low + high) / 2 * 10) / 10  # round to 0.1
            step = self._run_step(mid, TestPhase.BINARY_SEARCH, report)

            if step.passed:
                best = mid
                low = mid
            else:
                high = mid

            logger.info(
                "Binary search: low=%.1f high=%.1f mid=%.1f passed=%s",
                low, high, mid, step.passed,
            )

        return best

    # -- verification ------------------------------------------------------

    def _verify(self, report: CapacityReport, cps: float) -> bool:
        """Hold at discovered CPS for a longer dwell to confirm stability."""
        report.phase = TestPhase.VERIFICATION
        original_dwell = self.dwell_time
        self.dwell_time = self.verification_dwell
        try:
            step = self._run_step(cps, TestPhase.VERIFICATION, report)
            return step.passed
        finally:
            self.dwell_time = original_dwell

    # -- single step -------------------------------------------------------

    def _run_step(self, cps: float, phase: TestPhase, report: CapacityReport) -> StepResult:
        """Set the rate, dwell, collect metrics, evaluate."""
        self._step_counter += 1
        step = StepResult(step_number=self._step_counter, cps=cps, phase=phase)

        logger.debug("Step %d: setting CPS to %.2f", step.step_number, cps)
        if not self._rate_ctrl.set_rate(cps):
            step.passed = False
            step.failure_mode = FailureMode.ENGINE_ERROR
            report.steps.append(step)
            self._notify(phase, step)
            return step

        # Reset metrics and dwell
        self._metrics.reset()
        dwell_start = time.monotonic()
        remaining = self.dwell_time

        while remaining > 0 and not self._stop_event.is_set():
            sleep_chunk = min(remaining, 2.0)
            time.sleep(sleep_chunk)
            remaining = self.dwell_time - (time.monotonic() - dwell_start)

        step.dwell_seconds = time.monotonic() - dwell_start

        # Collect metrics
        m = self._metrics.collect()
        step.total_calls = int(m.get("total_calls", 0))
        step.successful_calls = int(m.get("successful_calls", 0))
        step.failed_calls = int(m.get("failed_calls", 0))
        step.retransmissions = int(m.get("retransmissions", 0))
        step.avg_response_time_ms = float(m.get("avg_response_time_ms", 0.0))

        total = step.successful_calls + step.failed_calls
        step.success_rate_pct = (step.successful_calls / total * 100.0) if total > 0 else 100.0
        step.retransmission_rate_pct = (
            (step.retransmissions / total * 100.0) if total > 0 else 0.0
        )

        # Evaluate against criteria
        step.passed, step.failure_mode = self._evaluate_step(step)

        report.steps.append(step)
        self._notify(phase, step)

        logger.info(
            "Step %d @ %.1f CPS: success=%.1f%% rt=%.1fms retx=%.1f%% => %s",
            step.step_number, cps, step.success_rate_pct,
            step.avg_response_time_ms, step.retransmission_rate_pct,
            "PASS" if step.passed else f"FAIL ({step.failure_mode.value})",
        )
        return step

    def _evaluate_step(self, step: StepResult) -> tuple[bool, FailureMode]:
        """Check a step against the success criteria."""
        c = self.criteria

        if step.success_rate_pct < c.min_success_rate_pct:
            return False, FailureMode.LOW_SUCCESS_RATE
        if step.avg_response_time_ms > c.max_response_time_ms:
            return False, FailureMode.HIGH_RESPONSE_TIME
        if step.retransmission_rate_pct > c.max_retransmission_rate_pct:
            return False, FailureMode.EXCESSIVE_RETRANSMISSIONS

        return True, FailureMode.NONE

    # -- helpers -----------------------------------------------------------

    def _notify(self, phase: TestPhase, step: StepResult) -> None:
        if self._callback:
            try:
                self._callback(phase, self._step_counter, step)
            except Exception:
                logger.debug("Progress callback error", exc_info=True)

    @staticmethod
    def _finalise(report: CapacityReport) -> None:
        report.completed_at = datetime.datetime.utcnow()
        if report.started_at and report.completed_at:
            report.duration_seconds = (
                report.completed_at - report.started_at
            ).total_seconds()
        logger.info("Capacity test %s: %s", report.phase.value, report.summary)


# ---------------------------------------------------------------------------
# Multi-scenario comparison
# ---------------------------------------------------------------------------

def compare_scenarios(
    reports: list[CapacityReport],
) -> ScenarioComparison:
    """
    Compare capacity reports from different scenarios.

    Ranks them by ``max_stable_cps`` and identifies the best/worst.
    """
    comparison = ScenarioComparison(created_at=datetime.datetime.utcnow())
    comparison.reports = list(reports)

    if reports:
        ranked = sorted(reports, key=lambda r: r.max_stable_cps, reverse=True)
        comparison.best_scenario = ranked[0].scenario_name
        comparison.worst_scenario = ranked[-1].scenario_name

    logger.info(
        "Scenario comparison: %d scenarios, best=%s worst=%s",
        len(reports), comparison.best_scenario, comparison.worst_scenario,
    )
    return comparison
