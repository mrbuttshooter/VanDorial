"""
GenCall Alert / Notification System.
Rule-based alerting with webhook delivery, cooldowns, silencing,
and Slack-compatible payloads. Monitors test metrics and fires
alerts when thresholds are breached.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import threading
import time
import urllib.request
import urllib.error
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger("gencall.alerts")


# ─── Alert Severity & State ──────────────────────────────────────────────────

class AlertSeverity(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertState(Enum):
    PENDING = "pending"       # condition detected, cooldown not expired
    FIRING = "firing"         # actively firing
    RESOLVED = "resolved"     # condition cleared
    SILENCED = "silenced"     # manually suppressed


class ComparisonOp(Enum):
    LT = "lt"       # less than
    LE = "le"       # less than or equal
    GT = "gt"       # greater than
    GE = "ge"       # greater than or equal
    EQ = "eq"       # equal
    NE = "ne"       # not equal

    def evaluate(self, actual: float, threshold: float) -> bool:
        if self == ComparisonOp.LT:
            return actual < threshold
        elif self == ComparisonOp.LE:
            return actual <= threshold
        elif self == ComparisonOp.GT:
            return actual > threshold
        elif self == ComparisonOp.GE:
            return actual >= threshold
        elif self == ComparisonOp.EQ:
            return actual == threshold
        elif self == ComparisonOp.NE:
            return actual != threshold
        return False


# ─── Alert Rule ───────────────────────────────────────────────────────────────

@dataclass
class AlertRule:
    """
    Defines when an alert should fire.
    Monitors a named metric against a threshold using a comparison operator.
    """

    rule_id: str
    name: str
    description: str = ""

    # What to check
    metric: str = ""              # e.g. "success_rate", "avg_response_time_ms", "failed_calls"
    comparison: ComparisonOp = ComparisonOp.LT
    threshold: float = 0.0

    # Behavior
    severity: AlertSeverity = AlertSeverity.WARNING
    cooldown_seconds: float = 300.0   # min time between repeated firings
    auto_resolve: bool = True         # resolve when condition clears
    consecutive_checks: int = 1       # must fail N consecutive checks before firing
    enabled: bool = True

    # Targeting
    test_id: Optional[str] = None     # None = global (all tests)
    tags: list[str] = field(default_factory=list)

    # Webhooks
    webhook_urls: list[str] = field(default_factory=list)
    slack_webhook_url: Optional[str] = None

    # Internal state
    _consecutive_failures: int = field(default=0, repr=False)
    _last_fired: Optional[datetime.datetime] = field(default=None, repr=False)
    _state: AlertState = field(default=AlertState.RESOLVED, repr=False)

    @property
    def state(self) -> AlertState:
        return self._state

    def evaluate(self, metrics: dict[str, float]) -> bool:
        """Check if the rule condition is met given current metrics."""
        if not self.enabled or self._state == AlertState.SILENCED:
            return False
        value = metrics.get(self.metric)
        if value is None:
            return False
        return self.comparison.evaluate(value, self.threshold)

    def check_cooldown(self) -> bool:
        """Returns True if enough time has passed since last firing."""
        if self._last_fired is None:
            return True
        elapsed = (datetime.datetime.utcnow() - self._last_fired).total_seconds()
        return elapsed >= self.cooldown_seconds

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "description": self.description,
            "metric": self.metric,
            "comparison": self.comparison.value,
            "threshold": self.threshold,
            "severity": self.severity.value,
            "cooldown_seconds": self.cooldown_seconds,
            "auto_resolve": self.auto_resolve,
            "consecutive_checks": self.consecutive_checks,
            "enabled": self.enabled,
            "test_id": self.test_id,
            "tags": self.tags,
            "webhook_urls": self.webhook_urls,
            "slack_webhook_url": self.slack_webhook_url,
            "state": self._state.value,
            "consecutive_failures": self._consecutive_failures,
            "last_fired": self._last_fired.isoformat() if self._last_fired else None,
        }


# ─── Alert Event ──────────────────────────────────────────────────────────────

@dataclass
class AlertEvent:
    """A single alert firing or resolution event."""

    event_id: str
    rule_id: str
    rule_name: str
    state: AlertState
    severity: AlertSeverity
    metric: str
    metric_value: float
    threshold: float
    comparison: str
    message: str
    timestamp: datetime.datetime = field(default_factory=datetime.datetime.utcnow)
    test_id: Optional[str] = None
    delivered: bool = False
    delivery_error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "state": self.state.value,
            "severity": self.severity.value,
            "metric": self.metric,
            "metric_value": self.metric_value,
            "threshold": self.threshold,
            "comparison": self.comparison,
            "message": self.message,
            "timestamp": self.timestamp.isoformat(),
            "test_id": self.test_id,
            "delivered": self.delivered,
            "delivery_error": self.delivery_error,
        }

    def to_slack_payload(self) -> dict:
        """Format as a Slack-compatible webhook payload."""
        color_map = {
            AlertSeverity.INFO: "#36a64f",
            AlertSeverity.WARNING: "#ff9900",
            AlertSeverity.CRITICAL: "#ff0000",
        }
        state_emoji = {
            AlertState.FIRING: ":rotating_light:",
            AlertState.RESOLVED: ":white_check_mark:",
            AlertState.SILENCED: ":mute:",
        }
        emoji = state_emoji.get(self.state, ":bell:")
        color = color_map.get(self.severity, "#cccccc")
        if self.state == AlertState.RESOLVED:
            color = "#36a64f"

        return {
            "attachments": [
                {
                    "color": color,
                    "fallback": self.message,
                    "title": f"{emoji} GenCall Alert: {self.rule_name}",
                    "text": self.message,
                    "fields": [
                        {"title": "State", "value": self.state.value.upper(), "short": True},
                        {"title": "Severity", "value": self.severity.value.upper(), "short": True},
                        {"title": "Metric", "value": f"{self.metric} = {self.metric_value}", "short": True},
                        {"title": "Threshold", "value": f"{self.comparison} {self.threshold}", "short": True},
                    ],
                    "footer": "GenCall Alert System",
                    "ts": int(self.timestamp.timestamp()),
                }
            ]
        }

    def to_webhook_payload(self) -> dict:
        """Generic webhook JSON payload."""
        return {
            "source": "gencall",
            "version": "2.0.0",
            "event": self.to_dict(),
        }


# ─── Alert Engine ─────────────────────────────────────────────────────────────

def _generate_id(prefix: str) -> str:
    ts = str(time.monotonic_ns())
    return f"{prefix}-{hashlib.sha256(ts.encode()).hexdigest()[:10]}"


class AlertEngine:
    """
    Monitors metrics, evaluates rules, fires alerts, and delivers webhooks.
    Thread-safe with background evaluation loop.
    """

    def __init__(
        self,
        check_interval: float = 10.0,
        history_limit: int = 500,
        webhook_timeout: float = 10.0,
    ):
        self._rules: dict[str, AlertRule] = {}
        self._lock = threading.RLock()
        self._running = False
        self._check_interval = check_interval
        self._thread: Optional[threading.Thread] = None
        self._history: deque[AlertEvent] = deque(maxlen=history_limit)
        self._webhook_timeout = webhook_timeout
        self._metrics_source: Optional[Callable[[], dict[str, float]]] = None
        self._listeners: list[Callable[[AlertEvent], Any]] = []

        logger.info("Alert engine initialized (check_interval=%.1fs)", check_interval)

    # ─── Rule Management ──────────────────────────────────────────────────

    def add_rule(self, rule: AlertRule) -> AlertRule:
        with self._lock:
            self._rules[rule.rule_id] = rule
        logger.info("Alert rule added: %s (%s)", rule.name, rule.rule_id)
        return rule

    def create_rule(
        self,
        name: str,
        metric: str,
        comparison: str,
        threshold: float,
        severity: str = "warning",
        cooldown_seconds: float = 300.0,
        webhook_urls: Optional[list[str]] = None,
        slack_webhook_url: Optional[str] = None,
        test_id: Optional[str] = None,
        consecutive_checks: int = 1,
        tags: Optional[list[str]] = None,
    ) -> AlertRule:
        """Convenience factory for creating and registering a rule."""
        rule = AlertRule(
            rule_id=_generate_id("rule"),
            name=name,
            metric=metric,
            comparison=ComparisonOp(comparison),
            threshold=threshold,
            severity=AlertSeverity(severity),
            cooldown_seconds=cooldown_seconds,
            webhook_urls=webhook_urls or [],
            slack_webhook_url=slack_webhook_url,
            test_id=test_id,
            consecutive_checks=consecutive_checks,
            tags=tags or [],
        )
        return self.add_rule(rule)

    def remove_rule(self, rule_id: str) -> bool:
        with self._lock:
            rule = self._rules.pop(rule_id, None)
            if rule:
                logger.info("Alert rule removed: %s", rule_id)
                return True
            return False

    def enable_rule(self, rule_id: str) -> bool:
        with self._lock:
            rule = self._rules.get(rule_id)
            if not rule:
                return False
            rule.enabled = True
            return True

    def disable_rule(self, rule_id: str) -> bool:
        with self._lock:
            rule = self._rules.get(rule_id)
            if not rule:
                return False
            rule.enabled = False
            return True

    def silence_rule(self, rule_id: str) -> bool:
        with self._lock:
            rule = self._rules.get(rule_id)
            if not rule:
                return False
            rule._state = AlertState.SILENCED
            logger.info("Alert rule silenced: %s", rule_id)
            return True

    def unsilence_rule(self, rule_id: str) -> bool:
        with self._lock:
            rule = self._rules.get(rule_id)
            if not rule:
                return False
            if rule._state == AlertState.SILENCED:
                rule._state = AlertState.RESOLVED
                rule._consecutive_failures = 0
                logger.info("Alert rule unsilenced: %s", rule_id)
            return True

    def get_rule(self, rule_id: str) -> Optional[AlertRule]:
        with self._lock:
            return self._rules.get(rule_id)

    def list_rules(
        self,
        state: Optional[AlertState] = None,
        severity: Optional[AlertSeverity] = None,
    ) -> list[dict]:
        with self._lock:
            rules = list(self._rules.values())
        if state:
            rules = [r for r in rules if r._state == state]
        if severity:
            rules = [r for r in rules if r.severity == severity]
        return [r.to_dict() for r in rules]

    # ─── Metrics Source ───────────────────────────────────────────────────

    def set_metrics_source(self, source: Callable[[], dict[str, float]]) -> None:
        """
        Register a callable that returns current metrics as a dict.
        Expected keys: success_rate, avg_response_time_ms, failed_calls,
        current_calls, calls_per_second, etc.
        """
        self._metrics_source = source

    # ─── Lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._check_loop, daemon=True, name="alert-engine"
        )
        self._thread.start()
        logger.info("Alert engine started")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=self._check_interval * 3)
        logger.info("Alert engine stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    # ─── Manual Evaluation ────────────────────────────────────────────────

    def evaluate(self, metrics: dict[str, float]) -> list[AlertEvent]:
        """Evaluate all rules against the provided metrics. Returns fired events."""
        events: list[AlertEvent] = []

        with self._lock:
            rules = list(self._rules.values())

        for rule in rules:
            event = self._evaluate_rule(rule, metrics)
            if event:
                events.append(event)

        return events

    def _evaluate_rule(self, rule: AlertRule, metrics: dict[str, float]) -> Optional[AlertEvent]:
        condition_met = rule.evaluate(metrics)
        metric_value = metrics.get(rule.metric, 0.0)

        if condition_met:
            rule._consecutive_failures += 1

            if rule._consecutive_failures < rule.consecutive_checks:
                return None  # Not enough consecutive failures yet

            if rule._state == AlertState.FIRING:
                # Already firing; check cooldown for re-notification
                if not rule.check_cooldown():
                    return None

            # Fire the alert
            rule._state = AlertState.FIRING
            rule._last_fired = datetime.datetime.utcnow()

            message = (
                f"ALERT [{rule.severity.value.upper()}]: {rule.name} - "
                f"{rule.metric} ({metric_value:.2f}) {rule.comparison.value} "
                f"threshold ({rule.threshold:.2f})"
            )

            event = AlertEvent(
                event_id=_generate_id("evt"),
                rule_id=rule.rule_id,
                rule_name=rule.name,
                state=AlertState.FIRING,
                severity=rule.severity,
                metric=rule.metric,
                metric_value=metric_value,
                threshold=rule.threshold,
                comparison=rule.comparison.value,
                message=message,
                test_id=rule.test_id,
            )

            self._record_event(event)
            self._deliver_webhooks(rule, event)
            return event

        else:
            # Condition cleared
            rule._consecutive_failures = 0

            if rule._state == AlertState.FIRING and rule.auto_resolve:
                rule._state = AlertState.RESOLVED
                message = (
                    f"RESOLVED: {rule.name} - "
                    f"{rule.metric} ({metric_value:.2f}) is now within threshold "
                    f"({rule.threshold:.2f})"
                )

                event = AlertEvent(
                    event_id=_generate_id("evt"),
                    rule_id=rule.rule_id,
                    rule_name=rule.name,
                    state=AlertState.RESOLVED,
                    severity=rule.severity,
                    metric=rule.metric,
                    metric_value=metric_value,
                    threshold=rule.threshold,
                    comparison=rule.comparison.value,
                    message=message,
                    test_id=rule.test_id,
                )

                self._record_event(event)
                self._deliver_webhooks(rule, event)
                return event

        return None

    # ─── Event History ────────────────────────────────────────────────────

    def _record_event(self, event: AlertEvent) -> None:
        with self._lock:
            self._history.append(event)
        for listener in self._listeners:
            try:
                listener(event)
            except Exception:
                logger.debug("Alert listener error", exc_info=True)
        logger.info("Alert event: %s", event.message)

    def get_history(
        self,
        rule_id: Optional[str] = None,
        state: Optional[AlertState] = None,
        severity: Optional[AlertSeverity] = None,
        limit: int = 50,
    ) -> list[dict]:
        with self._lock:
            history = list(self._history)
        if rule_id:
            history = [e for e in history if e.rule_id == rule_id]
        if state:
            history = [e for e in history if e.state == state]
        if severity:
            history = [e for e in history if e.severity == severity]
        return [e.to_dict() for e in history[-limit:]]

    def get_active_alerts(self) -> list[dict]:
        """Get all currently firing rules."""
        with self._lock:
            firing = [r for r in self._rules.values() if r._state == AlertState.FIRING]
        return [r.to_dict() for r in firing]

    def add_listener(self, callback: Callable[[AlertEvent], Any]) -> None:
        """Register a callback invoked for every alert event."""
        self._listeners.append(callback)

    # ─── Webhook Delivery ─────────────────────────────────────────────────

    def _deliver_webhooks(self, rule: AlertRule, event: AlertEvent) -> None:
        """Deliver webhook notifications in a background thread."""
        urls = list(rule.webhook_urls)
        slack_url = rule.slack_webhook_url

        if not urls and not slack_url:
            return

        thread = threading.Thread(
            target=self._send_webhooks,
            args=(urls, slack_url, event),
            daemon=True,
            name=f"webhook-{event.event_id[:8]}",
        )
        thread.start()

    def _send_webhooks(
        self,
        urls: list[str],
        slack_url: Optional[str],
        event: AlertEvent,
    ) -> None:
        generic_payload = json.dumps(event.to_webhook_payload()).encode("utf-8")

        for url in urls:
            try:
                req = urllib.request.Request(
                    url,
                    data=generic_payload,
                    headers={"Content-Type": "application/json", "User-Agent": "GenCall/2.0"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=self._webhook_timeout) as resp:
                    logger.debug("Webhook delivered to %s (status=%d)", url, resp.status)
                    event.delivered = True
            except Exception as exc:
                event.delivery_error = f"{url}: {exc}"
                logger.warning("Webhook delivery failed to %s: %s", url, exc)

        if slack_url:
            slack_payload = json.dumps(event.to_slack_payload()).encode("utf-8")
            try:
                req = urllib.request.Request(
                    slack_url,
                    data=slack_payload,
                    headers={"Content-Type": "application/json", "User-Agent": "GenCall/2.0"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=self._webhook_timeout) as resp:
                    logger.debug("Slack webhook delivered (status=%d)", resp.status)
                    event.delivered = True
            except Exception as exc:
                event.delivery_error = f"slack: {exc}"
                logger.warning("Slack webhook failed: %s", exc)

    # ─── Background Check Loop ────────────────────────────────────────────

    def _check_loop(self) -> None:
        logger.debug("Alert check loop started")
        while self._running:
            try:
                if self._metrics_source:
                    metrics = self._metrics_source()
                    self.evaluate(metrics)
            except Exception:
                logger.exception("Alert evaluation error")
            time.sleep(self._check_interval)
        logger.debug("Alert check loop exited")

    # ─── Serialization ────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        with self._lock:
            rules = [r.to_dict() for r in self._rules.values()]
            firing = sum(1 for r in self._rules.values() if r._state == AlertState.FIRING)
            silenced = sum(1 for r in self._rules.values() if r._state == AlertState.SILENCED)
        return {
            "running": self._running,
            "total_rules": len(rules),
            "firing": firing,
            "silenced": silenced,
            "check_interval": self._check_interval,
            "history_size": len(self._history),
            "rules": rules,
        }
