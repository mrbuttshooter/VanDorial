"""
VanDorial Fleet Controller — async node client (design §5, contract §J).

A thin async httpx wrapper around ONE worker node, given its base URL + API key.
Every request injects the `X-API-Key` header and targets `<address><path>`. The
client is resilient: callers get raised exceptions on transport/HTTP errors and
decide how to react (the aggregator marks nodes offline on failure).

Self-signed TLS: for Phase-2 dev the worker may present a self-signed cert, so
`verify` is configurable (default False, matching contract §J "allow verify=False
configurable").
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger("gencall.controller.node_client")

DEFAULT_TIMEOUT = 10.0


class NodeClient:
    """Async REST client for a single worker node."""

    def __init__(
        self,
        address: str,
        api_key: str = "",
        *,
        verify: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        # Normalise: store base URL without trailing slash so we can join paths
        # that start with "/".
        self.address = (address or "").rstrip("/")
        self.api_key = api_key or ""
        self.verify = verify
        self.timeout = timeout

    # ─── internals ──────────────────────────────────────────────────────────

    def _headers(self, extra: Optional[dict] = None) -> dict:
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        if extra:
            headers.update(extra)
        return headers

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.address}{path}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> httpx.Response:
        url = self._url(path)
        async with httpx.AsyncClient(verify=self.verify,
                                     timeout=timeout or self.timeout) as client:
            resp = await client.request(
                method.upper(), url,
                json=json, params=params, headers=self._headers(),
            )
            return resp

    async def _request_json(self, method: str, path: str, **kw) -> Any:
        resp = await self._request(method, path, **kw)
        resp.raise_for_status()
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # ─── high-level worker endpoints ────────────────────────────────────────

    async def health(self) -> dict:
        """GET /api/health (unauthenticated on the worker, but key is harmless)."""
        return await self._request_json("GET", "/api/health",
                                        timeout=min(self.timeout, 5.0))

    async def get_stats(self) -> dict:
        """GET /api/stats — current StatsSnapshot dict."""
        return await self._request_json("GET", "/api/stats")

    async def get_history(self, limit: int = 100) -> dict:
        """GET /api/stats/history?limit=N → {history:[...]}"""
        return await self._request_json("GET", "/api/stats/history",
                                        params={"limit": limit})

    async def list_tests(self) -> dict:
        """GET /api/tests → {tests:[...]} (used for reconcile)."""
        return await self._request_json("GET", "/api/tests")

    async def start_test(self, payload: dict) -> dict:
        """POST /api/tests/start with a StartTestRequest body."""
        return await self._request_json("POST", "/api/tests/start", json=payload)

    async def stop_test(self, test_id: str) -> dict:
        """POST /api/tests/{test_id}/stop"""
        return await self._request_json(
            "POST", f"/api/tests/{test_id}/stop")

    async def update_rate(self, test_id: str, rate: float) -> dict:
        """POST /api/tests/{test_id}/rate {call_rate:...}"""
        return await self._request_json(
            "POST", f"/api/tests/{test_id}/rate", json={"call_rate": rate})

    # ─── loop campaign endpoints (design §4.4) ──────────────────────────────

    async def start_loop(self, payload: dict) -> dict:
        """POST /api/loops with a StartLoopRequest body → {status, campaign}."""
        return await self._request_json("POST", "/api/loops", json=payload)

    async def stop_loop(self, campaign_id: str) -> dict:
        """POST /api/loops/{campaign_id}/stop → {status, campaign}."""
        return await self._request_json(
            "POST", f"/api/loops/{campaign_id}/stop")

    async def get_loop(self, campaign_id: str) -> dict:
        """GET /api/loops/{campaign_id} — live status incl. loop_stats."""
        return await self._request_json(
            "GET", f"/api/loops/{campaign_id}")

    # ─── generic passthrough proxy ──────────────────────────────────────────

    async def proxy(
        self,
        method: str,
        path: str,
        json: Any = None,
        params: Optional[dict] = None,
    ) -> httpx.Response:
        """Generic proxy: forward `method path` to the node, return the raw
        httpx.Response so the caller can relay status + body verbatim."""
        return await self._request(method, path, json=json, params=params)
