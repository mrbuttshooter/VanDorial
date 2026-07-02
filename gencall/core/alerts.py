"""
Operational webhook alerts.

The api_gateway has always shipped HMAC-signed WebhookCallback machinery that
nothing fired. This module gives it real events: the AlertNotifier is a
fire-and-forget queue + ONE daemon sender thread (house style: event-wait, no
busy loop), so an alert can never block an engine path, and a flapping source
can never spam the endpoint (per event+key throttle).

Events fired today:
  worker      uas_restarted, uas_restart_failed, campaigns_resumed,
              stray_processes_killed, loop_completion_low
  controller  node_online, node_offline

Config ([alerts] in gencall.cfg): webhook_url (empty = alerts off),
webhook_secret (HMAC-SHA256 in X-GenCall-Signature), events (comma list,
empty = all), min_interval_s (per event+key throttle), completion_min_pct
(0 = disabled).

Payload shape:
  {"event": "<name>", "timestamp": <unix>, "source": "<role@host>",
   "data": {...}}
"""

import json
import logging
import queue
import socket
import threading
import time
import urllib.request

from gencall.core.api_gateway import WebhookCallback

logger = logging.getLogger("gencall.alerts")

# Alerts on completion % need a minimum sample so a campaign's first few calls
# (completion legitimately 0 while the loop warms up) never page anyone.
COMPLETION_MIN_ANSWERED = 10


class AlertNotifier:
    """Queue + single sender thread for operational webhook events."""

    def __init__(self, url, secret="", events=None, min_interval_s=60.0,
                 completion_min_pct=0.0, source=""):
        self.callback = WebhookCallback(url=url, secret=secret or "")
        # Empty set = every event; otherwise an allow-list of event names.
        self.events = {e.strip() for e in (events or []) if e.strip()}
        self.min_interval_s = max(0.0, float(min_interval_s))
        self.completion_min_pct = float(completion_min_pct or 0.0)
        self.source = source or f"gencall@{socket.gethostname()}"
        self._last_sent: dict = {}          # (event, key) -> monotonic ts
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue(maxsize=256)
        self._stop = threading.Event()
        self._thread = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="alert-notifier")
        self._thread.start()

    def stop(self, timeout=5.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    # ── event intake (never blocks, never raises) ────────────────────────────

    def notify(self, event: str, data: dict | None = None, key: str = "") -> bool:
        """Queue an event for delivery. Returns True if it was accepted.

        Filtered silently when the event is not in the configured allow-list or
        the same (event, key) fired within min_interval_s. Dropped (with a log)
        if the queue is full — an unreachable endpoint must never grow memory.
        """
        if not self.callback.url:
            return False
        if self.events and event not in self.events:
            return False
        now = time.monotonic()
        throttle_key = (event, key)
        with self._lock:
            last = self._last_sent.get(throttle_key)
            if last is not None and (now - last) < self.min_interval_s:
                return False
            self._last_sent[throttle_key] = now
        payload = {
            "event": event,
            "timestamp": time.time(),
            "source": self.source,
            "data": data or {},
        }
        try:
            self._queue.put_nowait(payload)
            return True
        except queue.Full:
            logger.warning("Alert queue full; dropping %s event", event)
            return False

    def check_completion(self, snapshot: dict):
        """Fire loop_completion_low when a running campaign's completion %
        drops below [alerts] completion_min_pct (0 disables). Called with each
        LoopMatcher snapshot; throttled per campaign like every other event."""
        if self.completion_min_pct <= 0 or not snapshot:
            return
        answered = int(snapshot.get("answered_out") or 0)
        if answered < COMPLETION_MIN_ANSWERED:
            return
        pct = float(snapshot.get("completion_pct") or 0.0)
        if pct >= self.completion_min_pct:
            return
        cid = str(snapshot.get("campaign_id") or "")
        self.notify(
            "loop_completion_low",
            {"campaign_id": cid, "completion_pct": pct,
             "threshold_pct": self.completion_min_pct,
             "answered_out": answered},
            key=cid,
        )

    def make_node_status_listener(self):
        """Build an aggregator status listener that alerts ONLY on liveness
        transitions. The aggregator notifies listeners on any status change
        (version, active_tests, ...); this closure keeps the last-seen online
        flag per node and fires node_online / node_offline on flips only."""
        seen: dict = {}

        def _listener(status: dict):
            nid = status.get("node_id")
            online = bool(status.get("online"))
            prev = seen.get(nid)
            seen[nid] = online
            if prev is None or prev == online:
                return
            self.notify(
                "node_online" if online else "node_offline",
                {"node_id": nid, "version": status.get("version"),
                 "error": status.get("error")},
                key=str(nid),
            )

        return _listener

    # ── delivery (sender thread) ─────────────────────────────────────────────

    def _run(self):
        while not self._stop.is_set():
            try:
                payload = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._send(payload)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("Alert delivery failed: %s", e)

    def _send(self, payload: dict) -> bool:
        body = json.dumps(payload)
        headers = dict(self.callback.headers)
        headers["Content-Type"] = "application/json"
        headers["User-Agent"] = "GenCall/2.0"
        if self.callback.secret:
            headers["X-GenCall-Signature"] = \
                f"sha256={self.callback.sign_payload(body)}"
        try:
            req = urllib.request.Request(
                self.callback.url, data=body.encode(),
                headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                ok = resp.status < 400
                logger.info("Alert %s -> %s: %d", payload.get("event"),
                            self.callback.url, resp.status)
                return ok
        except Exception as e:
            logger.warning("Alert %s -> %s failed: %s", payload.get("event"),
                           self.callback.url, e)
            return False


def build_from_config(config, source: str = "") -> AlertNotifier | None:
    """Build (and return) a notifier from [alerts], or None when unconfigured."""
    url = config.alerts_webhook_url
    if not url:
        return None
    return AlertNotifier(
        url=url,
        secret=config.alerts_webhook_secret,
        events=config.alerts_events,
        min_interval_s=config.alerts_min_interval_s,
        completion_min_pct=config.alerts_completion_min_pct,
        source=source,
    )
