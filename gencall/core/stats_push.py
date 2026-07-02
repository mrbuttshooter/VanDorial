"""
Worker → controller stats push (opt-in, additive).

The controller polls every worker's GET /api/stats on a timer. When
[fleet] controller_url is set, this pusher instead emits the worker's stats
snapshot to the controller as it is produced, so the controller does not have to
poll. The poll stays as the fallback (the controller skips a node only while its
push is fresh), so a dead pusher degrades to the old behavior.

Design mirrors gencall.core.alerts.AlertNotifier: a single daemon sender thread
draining a queue, so a slow/unreachable controller never blocks the stats
collection thread. The queue is size-1 and coalesces to the LATEST snapshot —
stale stats are worthless, only the newest matters — so a stalled controller can
never build a backlog.

Auth reuses the shared fleet_token (sent as X-Fleet-Token); identity is the
worker's own advertised address, which the controller resolves to a node_id.
"""

import json
import logging
import queue
import threading
import urllib.request

logger = logging.getLogger("gencall.stats_push")

# Stop waiting for the controller after this long — the next snapshot supersedes
# a dropped one anyway, so a short timeout keeps the sender responsive.
SEND_TIMEOUT_S = 5.0


class StatsPusher:
    """Coalescing queue + one sender thread that POSTs stats to the controller."""

    def __init__(self, controller_url: str, token: str, address: str,
                 path: str = "/api/fleet/ingest/stats"):
        self.url = controller_url.rstrip("/") + path
        self.token = token or ""
        self.address = address
        # maxsize=1: we only ever want the freshest snapshot in flight.
        self._queue: queue.Queue = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._thread = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="stats-pusher")
        self._thread.start()

    def stop(self, timeout=5.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    # ── intake (the StatsEngine listener; never blocks, never raises) ────────

    def submit(self, snapshot):
        """StatsEngine listener. Coalesce to the latest snapshot: if one is
        already queued (the sender is mid-flight or the controller is slow),
        drop the old one and enqueue this newer snapshot instead."""
        try:
            data = snapshot.to_dict() if hasattr(snapshot, "to_dict") else dict(snapshot)
        except Exception:  # pragma: no cover - defensive
            return
        try:
            self._queue.put_nowait(data)
        except queue.Full:
            # Replace the stale queued snapshot with this fresher one.
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(data)
            except queue.Full:  # pragma: no cover - lost a race; next tick retries
                pass

    # ── delivery (sender thread) ─────────────────────────────────────────────

    def _run(self):
        while not self._stop.is_set():
            try:
                stats = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._send(stats)
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("Stats push failed: %s", e)

    def _send(self, stats: dict) -> bool:
        body = json.dumps({"address": self.address, "stats": stats})
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "GenCall/2.0",
        }
        if self.token:
            headers["X-Fleet-Token"] = self.token
        try:
            req = urllib.request.Request(
                self.url, data=body.encode(), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=SEND_TIMEOUT_S) as resp:
                return resp.status < 400
        except Exception as e:
            logger.debug("Stats push -> %s failed: %s", self.url, e)
            return False


def build_from_config(config, address: str):
    """Build a StatsPusher from [fleet] config, or None when push is disabled
    (no controller_url). ``address`` is the worker's own advertised base URL."""
    url = config.fleet_controller_url
    if not url:
        return None
    return StatsPusher(url, config.fleet_token, address)
