"""
VanDorial Fleet Controller — aggregation engine (design §5).

The FleetAggregator keeps one logical "connection" per ENABLED node. It polls
each node's `GET /api/stats` (~1 Hz) for telemetry and `GET /api/health` (~5 s)
for liveness, holds the latest snapshot per node, and recomputes the fleet
`aggregate` (sum across online nodes) plus `per_group` rollups each tick. It also
keeps a rolling history of the aggregate and exposes a rate-split helper for the
launch path.

Design notes / contract:
  - StatsSnapshot keys (statsKeys): timestamp, active_instances, total_calls,
    successful_calls, failed_calls, current_calls, calls_per_second,
    avg_response_time_ms, success_rate.
  - aggregate sums total/successful/failed/current/cps and active_instances,
    averages avg_response_time_ms over contributing nodes, and recomputes
    success_rate = successful/(successful+failed)*100 (matches worker _collect).
  - Resilient: a node that errors is marked offline and skipped; the loop
    continues. Offline nodes contribute `null` in per_node.
  - On each aggregate tick the aggregator invokes registered listeners with the
    full FleetStats dict (the WS hub bridges that onto the `fleet_stats` topic,
    mirroring the worker's stats listener → broadcast pattern).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Optional

from gencall.controller.node_client import NodeClient

logger = logging.getLogger("gencall.controller.aggregator")

# StatsSnapshot numeric fields that are summed across nodes.
_SUM_FIELDS = (
    "active_instances",
    "total_calls",
    "successful_calls",
    "failed_calls",
    "current_calls",
    "calls_per_second",
)

STATS_KEYS = [
    "timestamp", "active_instances", "total_calls", "successful_calls",
    "failed_calls", "current_calls", "calls_per_second",
    "avg_response_time_ms", "success_rate",
]


def empty_snapshot() -> dict:
    """A zeroed StatsSnapshot in the exact worker shape."""
    return {
        "timestamp": round(time.time(), 1),
        "active_instances": 0,
        "total_calls": 0,
        "successful_calls": 0,
        "failed_calls": 0,
        "current_calls": 0,
        "calls_per_second": 0.0,
        "avg_response_time_ms": 0.0,
        "success_rate": 0.0,
    }


def aggregate_snapshots(snapshots: list[dict]) -> dict:
    """Sum a list of per-node StatsSnapshot dicts into one aggregate snapshot.

    Mirrors the worker's `_collect`: sum the numeric fields, average response
    time over contributing nodes, recompute success_rate from totals.
    """
    out = empty_snapshot()
    if not snapshots:
        return out

    rt_sum = 0.0
    rt_count = 0
    for s in snapshots:
        if not s:
            continue
        for field in _SUM_FIELDS:
            out[field] += s.get(field, 0) or 0
        rt = s.get("avg_response_time_ms", 0) or 0
        if rt:
            rt_sum += rt
            rt_count += 1

    out["calls_per_second"] = round(out["calls_per_second"], 2)
    out["avg_response_time_ms"] = round(rt_sum / rt_count, 2) if rt_count else 0.0
    total = out["successful_calls"] + out["failed_calls"]
    out["success_rate"] = round((out["successful_calls"] / total) * 100, 2) if total else 0.0
    return out


def aggregate_loop_stats(per_node: dict[int, Optional[dict]]) -> dict:
    """Sum a map of node_id -> loop_stats snapshot into one combined view.

    Each per-node snapshot is the shape produced by the worker's
    ``LoopMatcher.latest_stats`` (design §4.3): calls_out, answered_out,
    minutes_out_ms, calls_in_matched, minutes_in_ms, completion_pct,
    delta_avg_ms, ... plus a ``failures`` dict ({out:{code:n}, in:{code:n}}).

    Combined semantics (design §7 stage 9, §4.3):
      - call / minute counters are summed across contributing nodes,
      - completion_pct is recomputed from the summed matched/out totals so it is
        a true fleet completion rather than an average of per-node percentages,
      - delta_avg_ms is averaged over contributing nodes (count-weighted is not
        available without the raw deltas; node-mean matches the per-node display),
      - failures-by-SIP-code are merged (summed per code) for out and in.
    """
    summed = {
        "calls_out": 0,
        "answered_out": 0,
        "minutes_out_ms": 0,
        "calls_in_matched": 0,
        "minutes_in_ms": 0,
    }
    failures_out: dict[str, int] = {}
    failures_in: dict[str, int] = {}
    delta_sum = 0.0
    delta_count = 0
    contributing = 0

    for snap in per_node.values():
        if not snap:
            continue
        contributing += 1
        for field in summed:
            summed[field] += snap.get(field, 0) or 0
        delta = snap.get("delta_avg_ms")
        if delta:
            delta_sum += delta
            delta_count += 1
        fails = snap.get("failures") or {}
        for code, n in (fails.get("out") or {}).items():
            failures_out[str(code)] = failures_out.get(str(code), 0) + (n or 0)
        for code, n in (fails.get("in") or {}).items():
            failures_in[str(code)] = failures_in.get(str(code), 0) + (n or 0)

    out = dict(summed)
    out["completion_pct"] = (
        round((summed["calls_in_matched"] / summed["calls_out"]) * 100, 2)
        if summed["calls_out"] else 0.0
    )
    out["delta_avg_ms"] = round(delta_sum / delta_count, 2) if delta_count else 0.0
    out["minutes_out"] = round(summed["minutes_out_ms"] / 60000.0, 4)
    out["minutes_in"] = round(summed["minutes_in_ms"] / 60000.0, 4)
    out["failures"] = {"out": failures_out, "in": failures_in}
    out["nodes_contributing"] = contributing
    return out


def split_rate(mode: str, value: float, n_targets: int) -> list[float]:
    """Compute the per-node call rate for `n_targets` online targets.

    - per_node (default): every target gets `value` cps.
    - total: split `value` evenly across targets, distributing the remainder to
      the first nodes (design §5).
    Returns a list of length n_targets (empty if no targets).
    """
    if n_targets <= 0:
        return []
    if (mode or "per_node") != "total":
        return [float(value)] * n_targets

    # total: even split with remainder to the first nodes. Work in a fixed-point
    # (hundredths of a cps) so we distribute fractional cps deterministically.
    units = int(round(float(value) * 100))
    base = units // n_targets
    remainder = units - base * n_targets
    rates = []
    for i in range(n_targets):
        u = base + (1 if i < remainder else 0)
        rates.append(round(u / 100.0, 2))
    return rates


class FleetAggregator:
    """Background poller + telemetry aggregator for the fleet.

    Runs an asyncio task loop in a dedicated thread (so it works regardless of
    the host app's loop). `node_provider` is a zero-arg callable returning the
    current list of enabled nodes; each node is an object/dict exposing
    `id`, `address`, `api_key`, `group_id`, and `enabled`.
    """

    def __init__(
        self,
        node_provider: Callable[[], list],
        *,
        stats_interval: float = 1.0,
        health_interval: float = 5.0,
        history_size: int = 1000,
        verify_tls: bool = False,
    ):
        self._node_provider = node_provider
        self.stats_interval = stats_interval
        self.health_interval = health_interval
        self.verify_tls = verify_tls

        # node_id -> latest StatsSnapshot dict (None if offline)
        self._node_stats: dict[int, Optional[dict]] = {}
        # node_id -> {online, version, active_tests, last_seen, error, group_id}
        self._node_status: dict[int, dict] = {}
        self._history: deque[dict] = deque(maxlen=history_size)
        self._lock = threading.Lock()

        self._stats_listeners: list[Callable[[dict], None]] = []
        self._status_listeners: list[Callable[[dict], None]] = []

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ─── listeners ──────────────────────────────────────────────────────────

    def add_stats_listener(self, cb: Callable[[dict], None]) -> None:
        """Register a callback invoked with the full FleetStats each tick."""
        self._stats_listeners.append(cb)

    def add_status_listener(self, cb: Callable[[dict], None]) -> None:
        """Register a callback invoked with {node_id, online, version,
        active_tests} whenever a node's liveness changes."""
        self._status_listeners.append(cb)

    # ─── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="fleet-aggregator")
        self._thread.start()
        logger.info("Fleet aggregator started (stats=%ss health=%ss)",
                    self.stats_interval, self.health_interval)

    def stop(self) -> None:
        self._running = False
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.create_task(self._stats_loop())
            self._loop.create_task(self._health_loop())
            self._loop.run_forever()
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    # ─── node helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _node_attr(node, key, default=None):
        if isinstance(node, dict):
            return node.get(key, default)
        return getattr(node, key, default)

    def _enabled_nodes(self) -> list:
        try:
            nodes = self._node_provider() or []
        except Exception:
            logger.exception("Fleet aggregator: node provider failed")
            return []
        return [n for n in nodes if self._node_attr(n, "enabled", True)]

    def _client_for(self, node) -> NodeClient:
        return NodeClient(
            self._node_attr(node, "address", ""),
            self._node_attr(node, "api_key", ""),
            verify=self.verify_tls,
        )

    # ─── polling loops ──────────────────────────────────────────────────────

    async def _stats_loop(self) -> None:
        while self._running:
            try:
                await self._poll_stats_once()
            except Exception:
                logger.exception("Fleet aggregator stats tick failed")
            await asyncio.sleep(self.stats_interval)

    async def _health_loop(self) -> None:
        while self._running:
            try:
                await self._poll_health_once()
            except Exception:
                logger.exception("Fleet aggregator health tick failed")
            await asyncio.sleep(self.health_interval)

    async def _poll_stats_once(self) -> None:
        nodes = self._enabled_nodes()

        async def fetch(node):
            nid = self._node_attr(node, "id")
            try:
                stats = await self._client_for(node).get_stats()
                return nid, stats, None
            except Exception as exc:
                return nid, None, str(exc)

        results = await asyncio.gather(*(fetch(n) for n in nodes)) if nodes else []

        with self._lock:
            present_ids = set()
            for nid, stats, err in results:
                present_ids.add(nid)
                if stats is not None:
                    self._node_stats[nid] = stats
                else:
                    # Stats unavailable → treat as no current snapshot.
                    self._node_stats[nid] = None
            # Drop nodes no longer in inventory.
            for nid in list(self._node_stats.keys()):
                if nid not in present_ids:
                    self._node_stats.pop(nid, None)

        fleet = self.get_fleet_stats()
        with self._lock:
            self._history.append(fleet["aggregate"])

        for cb in self._stats_listeners:
            try:
                cb(fleet)
            except Exception:
                logger.debug("fleet stats listener error", exc_info=True)

    async def _poll_health_once(self) -> None:
        nodes = self._enabled_nodes()

        async def probe(node):
            nid = self._node_attr(node, "id")
            try:
                h = await self._client_for(node).health()
                return nid, h, None
            except Exception as exc:
                return nid, None, str(exc)

        results = await asyncio.gather(*(probe(n) for n in nodes)) if nodes else []
        group_by_id = {self._node_attr(n, "id"): self._node_attr(n, "group_id")
                       for n in nodes}

        changes = []
        with self._lock:
            for nid, h, err in results:
                prev = self._node_status.get(nid, {})
                if h is not None:
                    status = {
                        "node_id": nid,
                        "online": True,
                        "version": h.get("version"),
                        "active_tests": h.get("active_tests", 0),
                        "last_seen": time.time(),
                        "error": None,
                        "group_id": group_by_id.get(nid),
                    }
                else:
                    status = {
                        "node_id": nid,
                        "online": False,
                        "version": prev.get("version"),
                        "active_tests": 0,
                        "last_seen": prev.get("last_seen"),
                        "error": err,
                        "group_id": group_by_id.get(nid),
                    }
                if prev.get("online") != status["online"] or \
                        prev.get("version") != status["version"] or \
                        prev.get("active_tests") != status["active_tests"]:
                    changes.append(status)
                self._node_status[nid] = status
            present_ids = {self._node_attr(n, "id") for n in nodes}
            for nid in list(self._node_status.keys()):
                if nid not in present_ids:
                    self._node_status.pop(nid, None)

        for status in changes:
            for cb in self._status_listeners:
                try:
                    cb(status)
                except Exception:
                    logger.debug("node status listener error", exc_info=True)

    # ─── snapshot accessors ─────────────────────────────────────────────────

    def is_online(self, node_id: int) -> bool:
        with self._lock:
            return bool(self._node_status.get(node_id, {}).get("online", False))

    def node_status(self, node_id: int) -> Optional[dict]:
        with self._lock:
            st = self._node_status.get(node_id)
            return dict(st) if st else None

    def get_fleet_stats(self) -> dict:
        """Return the current FleetStats: {aggregate, per_group, per_node}.

        per_node maps node_id -> StatsSnapshot | None (None = offline / no data).
        Only nodes considered online (latest snapshot present) contribute to the
        aggregate and per_group sums.
        """
        with self._lock:
            node_stats = dict(self._node_stats)
            status = dict(self._node_status)

        per_node: dict[int, Optional[dict]] = {}
        per_group_lists: dict[Any, list] = {}
        online_snaps: list[dict] = []

        for nid, snap in node_stats.items():
            online = bool(status.get(nid, {}).get("online", snap is not None))
            usable = snap if (snap is not None and online) else None
            per_node[nid] = usable
            if usable is not None:
                online_snaps.append(usable)
                gid = status.get(nid, {}).get("group_id")
                if gid is not None:
                    per_group_lists.setdefault(gid, []).append(usable)

        per_group = {gid: aggregate_snapshots(snaps)
                     for gid, snaps in per_group_lists.items()}

        return {
            "aggregate": aggregate_snapshots(online_snaps),
            "per_group": per_group,
            "per_node": per_node,
        }

    def get_history(self, limit: int = 240) -> list[dict]:
        with self._lock:
            data = list(self._history)
        if limit and limit > 0:
            data = data[-limit:]
        return data

    # ─── rate split (exposed for the launch path) ───────────────────────────

    @staticmethod
    def split_rate(mode: str, value: float, n_targets: int) -> list[float]:
        return split_rate(mode, value, n_targets)
