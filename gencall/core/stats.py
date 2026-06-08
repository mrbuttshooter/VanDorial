"""
GenCall Real-time Stats Engine.
Collects, aggregates, and serves live statistics from all running instances.
"""

import time
import threading
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from gencall.core.config import Config

logger = logging.getLogger("gencall.stats")


@dataclass
class StatsSnapshot:
    """A point-in-time snapshot of system stats."""
    timestamp: float = 0.0
    active_instances: int = 0
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    current_calls: int = 0
    calls_per_second: float = 0.0
    avg_response_time_ms: float = 0.0
    success_rate: float = 0.0

    def to_dict(self):
        return {
            "timestamp": round(self.timestamp, 1),
            "active_instances": self.active_instances,
            "total_calls": self.total_calls,
            "successful_calls": self.successful_calls,
            "failed_calls": self.failed_calls,
            "current_calls": self.current_calls,
            "calls_per_second": round(self.calls_per_second, 2),
            "avg_response_time_ms": round(self.avg_response_time_ms, 2),
            "success_rate": round(self.success_rate, 2),
        }


class StatsEngine:
    """
    Collects stats from the SIPp engine and maintains a time-series history.
    Provides data for the dashboard and API.
    """

    def __init__(self, config: Config = None):
        config = config or Config()
        self.interval = config.stats_interval
        self.history_size = config.stats_history_size
        self.history: deque[StatsSnapshot] = deque(maxlen=self.history_size)
        self.current = StatsSnapshot()
        self._sipp_engine = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._listeners: list = []

    def set_engine(self, sipp_engine):
        """Connect to the SIPp engine for stats collection."""
        self._sipp_engine = sipp_engine

    def start(self):
        """Start the stats collection loop."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._collect_loop, daemon=True, name="stats-engine")
        self._thread.start()
        logger.info("Stats engine started (interval=%ds)", self.interval)

    def stop(self):
        self._running = False

    def add_listener(self, callback):
        """Add a callback that receives StatsSnapshot on each collection."""
        self._listeners.append(callback)

    def get_current(self) -> dict:
        with self._lock:
            return self.current.to_dict()

    def get_history(self, limit: int = 0) -> list[dict]:
        with self._lock:
            data = list(self.history)
            if limit > 0:
                data = data[-limit:]
            return [s.to_dict() for s in data]

    def _collect_loop(self):
        while self._running:
            try:
                self._collect()
            except Exception:
                logger.exception("Stats collection error")
            time.sleep(self.interval)

    def _collect(self):
        if not self._sipp_engine:
            return

        snapshot = StatsSnapshot(timestamp=time.time())

        total_calls = 0
        successful = 0
        failed = 0
        current = 0
        cps_sum = 0.0
        rt_sum = 0.0
        active = 0

        for inst in self._sipp_engine.instances.values():
            if inst.state.value == "running":
                active += 1
                s = inst.stats
                total_calls += s.total_calls
                successful += s.successful_calls
                failed += s.failed_calls
                current += s.current_calls
                cps_sum += s.calls_per_second
                rt_sum += s.avg_response_time_ms

        snapshot.active_instances = active
        snapshot.total_calls = total_calls
        snapshot.successful_calls = successful
        snapshot.failed_calls = failed
        snapshot.current_calls = current
        snapshot.calls_per_second = cps_sum
        if active > 0:
            snapshot.avg_response_time_ms = rt_sum / active
        total = successful + failed
        if total > 0:
            snapshot.success_rate = (successful / total) * 100

        with self._lock:
            self.current = snapshot
            self.history.append(snapshot)

        for listener in self._listeners:
            try:
                listener(snapshot)
            except Exception:
                pass
