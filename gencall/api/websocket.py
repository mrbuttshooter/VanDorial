"""
GenCall - WebSocket API

Real-time data feeds via WebSocket:
  - Live stats stream (CPS, success rate, active calls)
  - Live CDR stream (call detail records as they complete)
  - Live SIP message stream (captured SIP traffic)
  - Live alert stream (firing/resolved alerts)
  - Per-test subscription (stats for a specific test instance)
"""

import asyncio
import json
import time
import logging
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

logger = logging.getLogger("gencall.api.websocket")

router = APIRouter()


class ConnectionManager:
    """
    Manages WebSocket connections and broadcasts data to subscribers.
    Each connection subscribes to one or more channels.
    """

    def __init__(self):
        # channel -> set of websocket connections
        self._channels: dict[str, set[WebSocket]] = {
            "stats": set(),
            "cdr": set(),
            "sip": set(),
            "alerts": set(),
            "logs": set(),
        }
        self._test_channels: dict[str, set[WebSocket]] = {}  # test_id -> connections
        self._lock = asyncio.Lock()

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
        """Remove a websocket from all channels."""
        async with self._lock:
            for channel_set in self._channels.values():
                channel_set.discard(ws)
            for channel_set in self._test_channels.values():
                channel_set.discard(ws)

    async def broadcast(self, channel: str, data: dict):
        """Send data to all subscribers of a channel."""
        dead = []
        subscribers = self._channels.get(channel, set()).copy()

        message = json.dumps({"channel": channel, "data": data, "ts": time.time()})

        for ws in subscribers:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)

        # Clean up dead connections
        if dead:
            async with self._lock:
                for ws in dead:
                    self._channels.get(channel, set()).discard(ws)

    async def broadcast_test(self, test_id: str, data: dict):
        """Send data to subscribers of a specific test."""
        dead = []
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

    def subscriber_count(self, channel: str) -> int:
        return len(self._channels.get(channel, set()))

    def to_dict(self) -> dict:
        return {
            "channels": {ch: len(subs) for ch, subs in self._channels.items()},
            "test_subscriptions": {tid: len(subs) for tid, subs in self._test_channels.items()},
        }


# Global connection manager
manager = ConnectionManager()


# ─── WebSocket Endpoints ──────────────────────────────────────────────────────

@router.websocket("/ws")
async def websocket_main(ws: WebSocket):
    """
    Main WebSocket endpoint. Clients send JSON commands to subscribe/unsubscribe:

    Subscribe to a channel:
        {"action": "subscribe", "channel": "stats"}
        {"action": "subscribe", "channel": "cdr"}
        {"action": "subscribe", "channel": "sip"}
        {"action": "subscribe", "channel": "alerts"}

    Subscribe to a specific test's stats:
        {"action": "subscribe_test", "test_id": "my-test-01"}

    Unsubscribe:
        {"action": "unsubscribe", "channel": "stats"}
    """
    await ws.accept()
    logger.info("WebSocket connected")

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_text(json.dumps({"error": "Invalid JSON"}))
                continue

            action = msg.get("action", "")

            if action == "subscribe":
                channel = msg.get("channel", "")
                if channel:
                    await manager.subscribe(ws, channel)
                    await ws.send_text(json.dumps({
                        "status": "subscribed", "channel": channel
                    }))

            elif action == "unsubscribe":
                channel = msg.get("channel", "")
                if channel:
                    await manager.unsubscribe(ws, channel)
                    await ws.send_text(json.dumps({
                        "status": "unsubscribed", "channel": channel
                    }))

            elif action == "subscribe_test":
                test_id = msg.get("test_id", "")
                if test_id:
                    await manager.subscribe_test(ws, test_id)
                    await ws.send_text(json.dumps({
                        "status": "subscribed", "channel": f"test:{test_id}"
                    }))

            elif action == "unsubscribe_test":
                test_id = msg.get("test_id", "")
                if test_id:
                    await manager.unsubscribe_test(ws, test_id)

            elif action == "ping":
                await ws.send_text(json.dumps({"action": "pong", "ts": time.time()}))

            elif action == "status":
                await ws.send_text(json.dumps({
                    "action": "status", "connections": manager.to_dict()
                }))

            else:
                await ws.send_text(json.dumps({"error": f"Unknown action: {action}"}))

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except Exception as e:
        logger.debug("WebSocket error: %s", e)
    finally:
        await manager.disconnect(ws)


@router.websocket("/ws/stats")
async def websocket_stats(ws: WebSocket):
    """Shortcut: auto-subscribe to stats channel."""
    await ws.accept()
    await manager.subscribe(ws, "stats")
    try:
        while True:
            await ws.receive_text()  # Keep connection alive
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(ws)


@router.websocket("/ws/cdr")
async def websocket_cdr(ws: WebSocket):
    """Shortcut: auto-subscribe to CDR channel."""
    await ws.accept()
    await manager.subscribe(ws, "cdr")
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(ws)


@router.websocket("/ws/sip")
async def websocket_sip(ws: WebSocket):
    """Shortcut: auto-subscribe to SIP debug channel."""
    await ws.accept()
    await manager.subscribe(ws, "sip")
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(ws)


# ─── Broadcast Helpers (called from other modules) ────────────────────────────

async def broadcast_stats(data: dict):
    """Broadcast stats update to all stats subscribers."""
    await manager.broadcast("stats", data)


async def broadcast_cdr(cdr_data: dict):
    """Broadcast a new CDR to all CDR subscribers."""
    await manager.broadcast("cdr", cdr_data)


async def broadcast_sip_message(msg_data: dict):
    """Broadcast a captured SIP message."""
    await manager.broadcast("sip", msg_data)


async def broadcast_alert(alert_data: dict):
    """Broadcast an alert event."""
    await manager.broadcast("alerts", alert_data)


async def broadcast_test_stats(test_id: str, data: dict):
    """Broadcast stats for a specific test instance."""
    await manager.broadcast_test(test_id, data)


# ─── REST endpoint for WS status ─────────────────────────────────────────────

@router.get("/api/ws/status")
async def ws_status():
    """Get WebSocket connection status."""
    return manager.to_dict()
