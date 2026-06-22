"""
VanDorial Fleet Controller — WebSocket hub (design §4 "WebSocket /ws").

Reuses the worker's subscribe protocol + outgoing STREAM envelope (see
gencall/api/websocket.py) so the existing frontend `stream` singleton works
unchanged against the controller. The controller publishes FLEET topics:

  - fleet_stats  — {aggregate, per_group, per_node}, pushed ~1 Hz.
  - node_status  — {node_id, online, version, active_tests} on change.
  - fleet_events — launch/stop/partial-failure notifications.
  - logs         — optional aggregated log lines.

Outgoing envelope (consumed by frontend ws.ts):
  {"type":"stream","channel":<topic>,"topic":<topic>,"data":<dict>,"ts":<float>}

The aggregator's stats/status listeners bridge into `broadcast_topic_sync`,
mirroring the worker's StatsEngine listener → broadcast pattern.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from enum import Enum
from typing import Optional

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from gencall.api.routes import require_api_key

logger = logging.getLogger("gencall.controller.ws")


# ─── Fleet Stream Topics ───────────────────────────────────────────────────────

class FleetTopic(Enum):
    FLEET_STATS = "fleet_stats"
    NODE_STATUS = "node_status"
    FLEET_EVENTS = "fleet_events"
    LOGS = "logs"
    ALL = "all"


# ─── Client connection tracker ─────────────────────────────────────────────────

class WSClient:
    """Tracks a single controller WebSocket client + its subscriptions."""

    _id_counter = 0
    _id_lock = threading.Lock()

    def __init__(self, websocket: WebSocket):
        with WSClient._id_lock:
            WSClient._id_counter += 1
            self.client_id = f"ws-{WSClient._id_counter}"
        self.websocket = websocket
        self.subscriptions: set[FleetTopic] = set()
        self.connected_at = time.time()
        self._send_lock = asyncio.Lock()

    async def send_json(self, data: dict) -> bool:
        try:
            async with self._send_lock:
                await self.websocket.send_json(data)
            return True
        except Exception:
            return False

    def is_subscribed(self, topic: FleetTopic) -> bool:
        if FleetTopic.ALL in self.subscriptions:
            return True
        return topic in self.subscriptions

    def to_dict(self) -> dict:
        return {
            "client_id": self.client_id,
            "subscriptions": sorted(s.value for s in self.subscriptions),
            "connected_at": self.connected_at,
            "uptime_seconds": round(time.time() - self.connected_at, 1),
        }


# ─── Connection manager ────────────────────────────────────────────────────────

class FleetConnectionManager:
    """Manages controller WebSocket connections and topic broadcasting."""

    def __init__(self):
        self._clients: dict[str, WSClient] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> WSClient:
        await websocket.accept()
        client = WSClient(websocket)
        async with self._lock:
            self._clients[client.client_id] = client
        logger.info("Controller WS client connected: %s", client.client_id)
        await client.send_json({
            "type": "connected",
            "client_id": client.client_id,
            "available_topics": [t.value for t in FleetTopic],
        })
        return client

    async def disconnect_client(self, client: WSClient) -> None:
        async with self._lock:
            self._clients.pop(client.client_id, None)
        logger.info("Controller WS client disconnected: %s", client.client_id)

    async def broadcast_topic(self, topic: FleetTopic, data: dict) -> int:
        message = {
            "type": "stream",
            "channel": topic.value,
            "topic": topic.value,
            "data": data,
            "ts": time.time(),
        }
        async with self._lock:
            clients = list(self._clients.values())

        delivered = 0
        failed: list[str] = []
        for client in clients:
            if client.is_subscribed(topic):
                if await client.send_json(message):
                    delivered += 1
                else:
                    failed.append(client.client_id)
        if failed:
            async with self._lock:
                for cid in failed:
                    self._clients.pop(cid, None)
        return delivered

    def broadcast_topic_sync(self, topic: FleetTopic, data: dict) -> None:
        """Synchronous broadcast for non-async callbacks (aggregator listeners)."""
        if not self._clients:
            return
        loop = _event_loop
        if loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.broadcast_topic(topic, data), loop)
        except RuntimeError:
            logger.debug("Cannot schedule controller broadcast: no running loop")

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def to_dict(self) -> dict:
        clients = list(self._clients.values())
        topic_counts: dict[str, int] = {}
        for topic in FleetTopic:
            topic_counts[topic.value] = sum(
                1 for c in clients if c.is_subscribed(topic))
        return {
            "total_clients": len(clients),
            "topic_subscribers": topic_counts,
            "clients": [c.to_dict() for c in clients],
        }


# ─── Global manager + event loop reference ─────────────────────────────────────

manager = FleetConnectionManager()

_event_loop: Optional[asyncio.AbstractEventLoop] = None


def set_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Set the asyncio loop for sync→async bridging (call in app startup)."""
    global _event_loop
    _event_loop = loop
    logger.info("Controller WebSocket bridge event loop set")


# ─── Subscription protocol ─────────────────────────────────────────────────────

async def _handle_client_message(client: WSClient, raw: str) -> dict:
    """Process a client message. Same subscribe protocol as the worker."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return {"type": "error", "error": "Invalid JSON"}

    action = msg.get("action", "")

    if action == "subscribe":
        topics = msg.get("topics", [])
        channel = msg.get("channel", "")
        subscribed: list[str] = []

        names = list(topics) if topics else ([channel] if channel else [])
        for t_name in names:
            try:
                topic = FleetTopic(t_name)
                client.subscriptions.add(topic)
                subscribed.append(topic.value)
            except ValueError:
                # Unknown topic (e.g. worker-only "stats") is silently ignored.
                pass

        return {
            "type": "subscribed",
            "status": "subscribed",
            "topics": subscribed,
            "channel": channel or (subscribed[0] if subscribed else ""),
        }

    elif action == "unsubscribe":
        topics = msg.get("topics", [])
        channel = msg.get("channel", "")
        unsubscribed: list[str] = []
        names = list(topics) if topics else ([channel] if channel else [])
        for t_name in names:
            try:
                topic = FleetTopic(t_name)
                client.subscriptions.discard(topic)
                unsubscribed.append(topic.value)
            except ValueError:
                pass
        return {
            "type": "unsubscribed",
            "status": "unsubscribed",
            "topics": unsubscribed,
            "channel": channel or (unsubscribed[0] if unsubscribed else ""),
        }

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

    return {
        "type": "error",
        "error": f"Unknown action: {action}",
        "supported_actions": ["subscribe", "unsubscribe", "ping", "status"],
    }


# ─── FastAPI router ─────────────────────────────────────────────────────────────

router = APIRouter(tags=["controller-websocket"])


async def _ws_authorized(ws: WebSocket) -> bool:
    """Validate the API key on the controller WS handshake (key via the
    ``api_key`` query param; header also accepted). Mirrors the worker hub —
    the fleet streams are no longer open to anyone who can reach the port."""
    from gencall.api import routes as _routes
    gw = getattr(_routes, "gateway", None)
    if gw is None:
        return True
    key = ws.query_params.get("api_key") or ws.headers.get("x-api-key")
    return bool(key and gw.keys.validate_key(key))


@router.websocket("/ws")
async def websocket_main(ws: WebSocket):
    """Controller WebSocket. Clients send JSON commands to manage subscriptions
    over the fleet topics (fleet_stats, node_status, fleet_events, logs)."""
    if not await _ws_authorized(ws):
        await ws.close(code=1008)
        return
    client = await manager.connect(ws)
    try:
        while True:
            raw = await ws.receive_text()
            response = await _handle_client_message(client, raw)
            await client.send_json(response)
    except WebSocketDisconnect:
        logger.info("Controller WS disconnected: %s", client.client_id)
    except Exception as exc:
        logger.debug("Controller WS error for %s: %s", client.client_id, exc)
    finally:
        await manager.disconnect_client(client)


@router.get("/api/ws/status", dependencies=[Depends(require_api_key)])
async def ws_status():
    return manager.to_dict()


# ─── Bridge helpers (registered as aggregator listeners) ───────────────────────

def on_fleet_stats(fleet_stats: dict) -> None:
    """Aggregator stats listener → fleet_stats topic."""
    manager.broadcast_topic_sync(FleetTopic.FLEET_STATS, fleet_stats)


def on_node_status(status: dict) -> None:
    """Aggregator status listener → node_status topic."""
    manager.broadcast_topic_sync(FleetTopic.NODE_STATUS, status)


def emit_fleet_event(event: dict) -> None:
    """Publish a launch/stop/partial-failure notification on fleet_events."""
    manager.broadcast_topic_sync(FleetTopic.FLEET_EVENTS, event)


def emit_log(line: dict) -> None:
    """Publish an aggregated log line on the logs topic."""
    manager.broadcast_topic_sync(FleetTopic.LOGS, line)
