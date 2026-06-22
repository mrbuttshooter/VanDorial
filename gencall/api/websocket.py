"""
GenCall WebSocket API.

Real-time streaming over a FastAPI WebSocket hub with per-topic subscription
support and a sync→async bridge for listener callbacks.

Live topics (design §4.4): ``stats`` (engine stats snapshots), ``loops`` (per-
campaign loop_stats from the LoopMatcher), ``logs``, and per-``test`` streams.
The never-fed ``cdr`` / ``sip`` / ``alerts`` topics and their dead broadcast
plumbing were removed in the v2 loop-runner work — what exists here is fed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from enum import Enum
from typing import Any, Optional

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from gencall.api.routes import require_api_key

logger = logging.getLogger("gencall.api.websocket")


# ─── Stream Topics ────────────────────────────────────────────────────────────

class StreamTopic(Enum):
    STATS = "stats"
    LOOPS = "loops"     # per-campaign loop_stats (fed by LoopMatcher, §4.3)
    LOGS = "logs"
    TEST = "test"       # per-test stats (requires test_id)
    ALL = "all"         # subscribe to everything


# ─── Client Connection Tracker ────────────────────────────────────────────────

class WSClient:
    """Tracks a single WebSocket client and its subscriptions."""

    _id_counter = 0
    _id_lock = threading.Lock()

    def __init__(self, websocket: WebSocket):
        with WSClient._id_lock:
            WSClient._id_counter += 1
            self.client_id = f"ws-{WSClient._id_counter}"
        self.websocket = websocket
        self.subscriptions: set[StreamTopic] = set()
        self.test_ids: set[str] = set()
        self.connected_at = time.time()
        self._send_lock = asyncio.Lock()

    async def send_json(self, data: dict) -> bool:
        """Send JSON to this client. Returns False if send fails."""
        try:
            async with self._send_lock:
                await self.websocket.send_json(data)
            return True
        except Exception:
            return False

    async def send_text(self, text: str) -> bool:
        try:
            async with self._send_lock:
                await self.websocket.send_text(text)
            return True
        except Exception:
            return False

    def is_subscribed(self, topic: StreamTopic, test_id: Optional[str] = None) -> bool:
        if StreamTopic.ALL in self.subscriptions:
            return True
        if topic not in self.subscriptions:
            return False
        if topic == StreamTopic.TEST and test_id:
            return test_id in self.test_ids or len(self.test_ids) == 0
        return True

    def to_dict(self) -> dict:
        return {
            "client_id": self.client_id,
            "subscriptions": sorted(s.value for s in self.subscriptions),
            "test_ids": sorted(self.test_ids),
            "connected_at": self.connected_at,
            "uptime_seconds": round(time.time() - self.connected_at, 1),
        }


# ─── Connection Manager ──────────────────────────────────────────────────────

class ConnectionManager:
    """Manages all WebSocket connections and message broadcasting."""

    def __init__(self):
        self._clients: dict[str, WSClient] = {}
        self._lock = asyncio.Lock()
        # Legacy channel-based sets (backward compat with old API). Only the
        # live topics remain — the never-fed cdr/sip/alerts channels were removed
        # with their broadcast plumbing in the v2 loop-runner work.
        self._channels: dict[str, set[WebSocket]] = {
            "stats": set(),
            "loops": set(),
            "logs": set(),
        }
        self._test_channels: dict[str, set[WebSocket]] = {}

    # ─── New client-based API ─────────────────────────────────────────────

    async def connect(self, websocket: WebSocket) -> WSClient:
        await websocket.accept()
        client = WSClient(websocket)
        async with self._lock:
            self._clients[client.client_id] = client
        logger.info("WebSocket client connected: %s", client.client_id)
        await client.send_json({
            "type": "connected",
            "client_id": client.client_id,
            "available_topics": [t.value for t in StreamTopic],
        })
        return client

    async def disconnect_client(self, client: WSClient) -> None:
        async with self._lock:
            self._clients.pop(client.client_id, None)
            # Also remove from legacy channels
            for channel_set in self._channels.values():
                channel_set.discard(client.websocket)
            for channel_set in self._test_channels.values():
                channel_set.discard(client.websocket)
        logger.info("WebSocket client disconnected: %s", client.client_id)

    async def broadcast_topic(
        self,
        topic: StreamTopic,
        data: dict,
        test_id: Optional[str] = None,
    ) -> int:
        """Broadcast to all clients subscribed to the topic. Returns delivery count."""
        message = {
            "type": "stream",
            "channel": topic.value,
            "topic": topic.value,
            "data": data,
            "ts": time.time(),
        }
        if test_id:
            message["test_id"] = test_id

        async with self._lock:
            clients = list(self._clients.values())

        delivered = 0
        failed: list[str] = []

        for client in clients:
            if client.is_subscribed(topic, test_id):
                ok = await client.send_json(message)
                if ok:
                    delivered += 1
                else:
                    failed.append(client.client_id)

        if failed:
            async with self._lock:
                for cid in failed:
                    self._clients.pop(cid, None)
            logger.debug("Removed %d dead WebSocket connections", len(failed))

        return delivered

    def broadcast_topic_sync(
        self,
        topic: StreamTopic,
        data: dict,
        test_id: Optional[str] = None,
    ) -> None:
        """
        Synchronous broadcast for use from non-async callbacks
        (stats listeners, CDR listeners, alert listeners, SIP listeners).
        """
        if not self._clients:
            return

        loop = _event_loop
        if loop is None:
            return

        try:
            asyncio.run_coroutine_threadsafe(
                self.broadcast_topic(topic, data, test_id),
                loop,
            )
        except RuntimeError:
            logger.debug("Cannot schedule broadcast: event loop not running")

    # ─── Legacy channel-based API (backward compat) ───────────────────────

    async def subscribe(self, ws: WebSocket, channel: str):
        async with self._lock:
            if channel not in self._channels:
                self._channels[channel] = set()
            self._channels[channel].add(ws)
        logger.debug("WS subscribed to %s (total: %d)", channel, len(self._channels[channel]))

    async def unsubscribe(self, ws: WebSocket, channel: str):
        async with self._lock:
            if channel in self._channels:
                self._channels[channel].discard(ws)

    async def subscribe_test(self, ws: WebSocket, test_id: str):
        async with self._lock:
            if test_id not in self._test_channels:
                self._test_channels[test_id] = set()
            self._test_channels[test_id].add(ws)

    async def unsubscribe_test(self, ws: WebSocket, test_id: str):
        async with self._lock:
            if test_id in self._test_channels:
                self._test_channels[test_id].discard(ws)

    async def disconnect(self, ws: WebSocket):
        """Remove a websocket from all legacy channels."""
        async with self._lock:
            for channel_set in self._channels.values():
                channel_set.discard(ws)
            for channel_set in self._test_channels.values():
                channel_set.discard(ws)

    async def broadcast(self, channel: str, data: dict):
        """Send data to all legacy subscribers of a channel."""
        dead: list[WebSocket] = []
        subscribers = self._channels.get(channel, set()).copy()
        message = json.dumps({"channel": channel, "data": data, "ts": time.time()})

        for ws in subscribers:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)

        if dead:
            async with self._lock:
                for ws in dead:
                    self._channels.get(channel, set()).discard(ws)

        # Also broadcast via new topic system
        try:
            topic = StreamTopic(channel)
            await self.broadcast_topic(topic, data)
        except ValueError:
            pass

    async def broadcast_test(self, test_id: str, data: dict):
        """Send data to subscribers of a specific test (legacy + new)."""
        dead: list[WebSocket] = []
        subscribers = self._test_channels.get(test_id, set()).copy()
        message = json.dumps({"channel": f"test:{test_id}", "data": data, "ts": time.time()})

        for ws in subscribers:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)

        if dead:
            async with self._lock:
                for ws in dead:
                    self._test_channels.get(test_id, set()).discard(ws)

        # Also broadcast via new topic system
        await self.broadcast_topic(StreamTopic.TEST, data, test_id=test_id)

    def subscriber_count(self, channel: str) -> int:
        return len(self._channels.get(channel, set()))

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def list_clients(self) -> list[dict]:
        # This is sync-safe since we only read
        return [c.to_dict() for c in list(self._clients.values())]

    def to_dict(self) -> dict:
        clients = list(self._clients.values())
        topic_counts: dict[str, int] = {}
        for topic in StreamTopic:
            count = sum(1 for c in clients if c.is_subscribed(topic))
            topic_counts[topic.value] = count
        return {
            "total_clients": len(clients),
            "channels": {ch: len(subs) for ch, subs in self._channels.items()},
            "test_subscriptions": {tid: len(subs) for tid, subs in self._test_channels.items()},
            "topic_subscribers": topic_counts,
            "clients": [c.to_dict() for c in clients],
        }


# ─── Global Manager Instance ─────────────────────────────────────────────────

manager = ConnectionManager()

# ─── Event Loop Reference (for sync-to-async bridging) ───────────────────────

_event_loop: Optional[asyncio.AbstractEventLoop] = None


def set_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Set the asyncio event loop for sync-to-async bridging.
    Call this once during app startup (e.g. in a FastAPI startup event).
    """
    global _event_loop
    _event_loop = loop
    logger.info("WebSocket bridge event loop set")


# ─── Subscription Protocol Handling ───────────────────────────────────────────

async def _handle_client_message(client: WSClient, raw: str) -> dict:
    """
    Process incoming client messages.
    Supported protocols:

    New (topic-based):
        {"action": "subscribe",   "topics": ["stats", "loops"]}
        {"action": "unsubscribe", "topics": ["loops"]}
        {"action": "subscribe",   "topics": ["test"], "test_ids": ["test-abc"]}

    Legacy (channel-based, backward compat):
        {"action": "subscribe",   "channel": "stats"}
        {"action": "unsubscribe", "channel": "stats"}
        {"action": "subscribe_test", "test_id": "my-test-01"}

    Common:
        {"action": "ping"}
        {"action": "status"}
    """
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return {"type": "error", "error": "Invalid JSON"}

    action = msg.get("action", "")

    if action == "subscribe":
        # New topic-based
        topics = msg.get("topics", [])
        test_ids = msg.get("test_ids", [])
        # Legacy channel-based
        channel = msg.get("channel", "")

        subscribed: list[str] = []

        if topics:
            for t_name in topics:
                try:
                    topic = StreamTopic(t_name)
                    client.subscriptions.add(topic)
                    subscribed.append(topic.value)
                except ValueError:
                    pass
            for tid in test_ids:
                client.test_ids.add(tid)
        elif channel:
            # Legacy: map channel name to topic
            try:
                topic = StreamTopic(channel)
                client.subscriptions.add(topic)
                subscribed.append(channel)
            except ValueError:
                pass
            # Also register in legacy manager
            await manager.subscribe(client.websocket, channel)

        return {
            "type": "subscribed",
            "status": "subscribed",
            "topics": subscribed,
            "channel": channel or (subscribed[0] if subscribed else ""),
            "test_ids": sorted(client.test_ids),
        }

    elif action == "unsubscribe":
        topics = msg.get("topics", [])
        channel = msg.get("channel", "")
        unsubscribed: list[str] = []

        if topics:
            for t_name in topics:
                try:
                    topic = StreamTopic(t_name)
                    client.subscriptions.discard(topic)
                    unsubscribed.append(topic.value)
                except ValueError:
                    pass
            for tid in msg.get("test_ids", []):
                client.test_ids.discard(tid)
        elif channel:
            try:
                topic = StreamTopic(channel)
                client.subscriptions.discard(topic)
                unsubscribed.append(channel)
            except ValueError:
                pass
            await manager.unsubscribe(client.websocket, channel)

        return {
            "type": "unsubscribed",
            "status": "unsubscribed",
            "topics": unsubscribed,
            "channel": channel or (unsubscribed[0] if unsubscribed else ""),
        }

    elif action == "subscribe_test":
        test_id = msg.get("test_id", "")
        if test_id:
            client.subscriptions.add(StreamTopic.TEST)
            client.test_ids.add(test_id)
            await manager.subscribe_test(client.websocket, test_id)
            return {
                "type": "subscribed",
                "status": "subscribed",
                "channel": f"test:{test_id}",
                "topics": ["test"],
                "test_ids": sorted(client.test_ids),
            }
        return {"type": "error", "error": "Missing test_id"}

    elif action == "unsubscribe_test":
        test_id = msg.get("test_id", "")
        if test_id:
            client.test_ids.discard(test_id)
            await manager.unsubscribe_test(client.websocket, test_id)
            return {
                "type": "unsubscribed",
                "status": "unsubscribed",
                "channel": f"test:{test_id}",
            }
        return {"type": "error", "error": "Missing test_id"}

    elif action == "ping":
        return {
            "type": "pong",
            "action": "pong",
            "ts": time.time(),
            "client_id": client.client_id,
        }

    elif action == "status":
        return {
            "type": "status",
            "action": "status",
            "client": client.to_dict(),
            "connections": manager.to_dict(),
        }

    else:
        return {
            "type": "error",
            "error": f"Unknown action: {action}",
            "supported_actions": [
                "subscribe", "unsubscribe", "subscribe_test",
                "unsubscribe_test", "ping", "status",
            ],
        }


# ─── FastAPI Router ───────────────────────────────────────────────────────────

router = APIRouter(tags=["websocket"])


@router.websocket("/ws")
async def websocket_main(ws: WebSocket):
    """
    Main WebSocket endpoint. Clients send JSON commands to manage subscriptions.

    New protocol (topic-based, supports multi-subscribe):
        {"action": "subscribe", "topics": ["stats", "loops"]}
        {"action": "subscribe", "topics": ["test"], "test_ids": ["my-test-1"]}
        {"action": "subscribe", "topics": ["all"]}

    Legacy protocol (channel-based, backward compatible):
        {"action": "subscribe", "channel": "stats"}
        {"action": "subscribe_test", "test_id": "my-test-01"}
    """
    client = await manager.connect(ws)
    try:
        while True:
            raw = await ws.receive_text()
            response = await _handle_client_message(client, raw)
            await client.send_json(response)
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected: %s", client.client_id)
    except Exception as exc:
        logger.debug("WebSocket error for %s: %s", client.client_id, exc)
    finally:
        await manager.disconnect_client(client)


@router.websocket("/ws/stats")
async def websocket_stats(ws: WebSocket):
    """Convenience endpoint: auto-subscribes to stats."""
    client = await manager.connect(ws)
    client.subscriptions.add(StreamTopic.STATS)
    await manager.subscribe(ws, "stats")
    try:
        while True:
            raw = await ws.receive_text()
            response = await _handle_client_message(client, raw)
            await client.send_json(response)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await manager.disconnect_client(client)


@router.websocket("/ws/loops")
async def websocket_loops(ws: WebSocket):
    """Convenience endpoint: auto-subscribes to the loop_stats stream (§4.3)."""
    client = await manager.connect(ws)
    client.subscriptions.add(StreamTopic.LOOPS)
    await manager.subscribe(ws, "loops")
    try:
        while True:
            raw = await ws.receive_text()
            response = await _handle_client_message(client, raw)
            await client.send_json(response)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await manager.disconnect_client(client)


# ─── REST Endpoints for WS Status ────────────────────────────────────────────

@router.get("/api/ws/status", dependencies=[Depends(require_api_key)])
async def ws_status():
    """Get WebSocket connection status."""
    return manager.to_dict()


@router.get("/api/ws/clients", dependencies=[Depends(require_api_key)])
async def ws_clients():
    """List connected WebSocket clients."""
    return {"clients": manager.list_clients()}


# ─── Async Broadcast Helpers (called from other async modules) ────────────────

async def broadcast_stats(data: dict):
    """Broadcast stats update to all stats subscribers."""
    await manager.broadcast("stats", data)


async def broadcast_loop_stats(data: dict):
    """Broadcast a loop_stats snapshot to all loops subscribers (§4.3)."""
    await manager.broadcast("loops", data)


async def broadcast_test_stats(test_id: str, data: dict):
    """Broadcast stats for a specific test instance."""
    await manager.broadcast_test(test_id, data)


# ─── Sync Bridge Functions ───────────────────────────────────────────────────
#
# These are callback-style functions designed to be registered as listeners
# on the StatsEngine and the LoopMatcher. They bridge synchronous listener
# callbacks into async WebSocket broadcasts.
#

def on_stats_update(snapshot: Any) -> None:
    """Bridge for StatsEngine.add_listener(). Broadcasts stats snapshots."""
    data = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
    manager.broadcast_topic_sync(StreamTopic.STATS, data)


def on_loop_stats(stats: Any) -> None:
    """Bridge for LoopMatcher(on_stats=...). Broadcasts loop_stats snapshots.

    The matcher hands a plain stats dict (campaign_id, out/in minutes,
    completion %, per-call delta, failures by code); we relay it on the
    ``loops`` topic so the console's Loops page updates live (design §4.3/§4.4).
    """
    data = stats.to_dict() if hasattr(stats, "to_dict") else stats
    manager.broadcast_topic_sync(StreamTopic.LOOPS, data)


def on_test_stats(test_id: str, stats: Any) -> None:
    """Broadcast per-test stats. Call manually from the stats collection loop."""
    data = stats.to_dict() if hasattr(stats, "to_dict") else stats
    manager.broadcast_topic_sync(StreamTopic.TEST, data, test_id=test_id)
