"""
Tests for the VanDorial Fleet Controller REST API.

These exercise gencall.controller.app.create_controller_app via FastAPI's
TestClient with the node client fully MOCKED — NodeClient's async HTTP methods
are monkeypatched so no real worker (and no live sipp) is ever required. This
sandbox is Windows without sipp; nothing here touches the engine or a binary.

Coverage:
  * Node CRUD (create / list / update / delete / 404s).
  * Group CRUD + membership reassignment.
  * split_rate helper (per_node gives each node N; total splits N across the
    online nodes, distributing the remainder to the first nodes).
  * POST /api/fleet/launch fan-out with a partial failure (one node errors →
    run status 'partial'; dispatched reflects per-node ok/error).
  * GET /api/fleet/stats aggregation (sum across mocked per-node snapshots).
  * Auth required: every protected endpoint is 401 without X-API-Key;
    /api/health stays open.
"""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def controller(monkeypatch):
    """Build a controller app backed by a throwaway sqlite DB (auth enabled),
    with NodeClient mocked and the aggregator's liveness/stats made
    deterministic. Yields (TestClient, headers, ctx) where ctx exposes helpers
    for driving node online-state and per-node stats from individual tests.
    """
    # Fresh Config singleton so our env-driven DB url is honored.
    from gencall.core.config import Config
    Config.reset()

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_url = "sqlite:///" + tmp.name
    monkeypatch.setenv("GENCALL_CONTROLLER_DATABASE_URL", db_url)

    # ── Mock NodeClient so no real worker / sipp is needed ──────────────────
    # Per-test mutable view of the fleet: node_id -> {"online", "stats", "raise"}.
    fleet: dict[int, dict] = {}
    # Record of start_test dispatches so launch tests can assert the fan-out.
    dispatched_calls: list[dict] = []
    # Record of start_loop dispatches so loop-launch tests can assert the fan-out.
    loop_dispatched_calls: list[dict] = []

    from gencall.controller import node_client as node_client_mod
    NodeClient = node_client_mod.NodeClient

    def _node_id_for(address: str):
        # Tests encode the node id in the address as http://node-<id> so a mocked
        # client (which only knows address + key) can resolve which node it is.
        for nid, spec in fleet.items():
            if spec.get("address") == address:
                return nid
        return None

    async def fake_health(self):
        nid = _node_id_for(self.address)
        spec = fleet.get(nid, {})
        if not spec.get("online", False):
            raise RuntimeError("connection refused")
        return {"version": "2.0.0", "active_tests": spec.get("active_tests", 0),
                "status": "ok"}

    async def fake_get_stats(self):
        nid = _node_id_for(self.address)
        spec = fleet.get(nid, {})
        if not spec.get("online", False):
            raise RuntimeError("connection refused")
        return dict(spec.get("stats") or {})

    async def fake_start_test(self, payload):
        nid = _node_id_for(self.address)
        spec = fleet.get(nid, {})
        dispatched_calls.append({"node_id": nid, "payload": payload})
        if spec.get("start_raises"):
            raise RuntimeError("worker rejected start")
        return {"id": f"test-{nid}"}

    async def fake_stop_test(self, test_id):
        return {"status": "stopped", "id": test_id}

    async def fake_start_loop(self, payload):
        nid = _node_id_for(self.address)
        spec = fleet.get(nid, {})
        loop_dispatched_calls.append({"node_id": nid, "payload": payload})
        if spec.get("start_raises"):
            raise RuntimeError("worker rejected loop start")
        return {"status": "started", "campaign": {"id": f"camp-{nid}"}}

    async def fake_stop_loop(self, campaign_id):
        loop_dispatched_calls.append({"stop_campaign": campaign_id})
        return {"status": "stopped", "campaign": {"id": campaign_id}}

    async def fake_get_loop(self, campaign_id):
        nid = _node_id_for(self.address)
        spec = fleet.get(nid, {})
        return {"id": campaign_id, "status": "running",
                "loop_stats": spec.get("loop_stats")}

    monkeypatch.setattr(NodeClient, "health", fake_health, raising=True)
    monkeypatch.setattr(NodeClient, "get_stats", fake_get_stats, raising=True)
    monkeypatch.setattr(NodeClient, "start_test", fake_start_test, raising=True)
    monkeypatch.setattr(NodeClient, "stop_test", fake_stop_test, raising=True)
    monkeypatch.setattr(NodeClient, "start_loop", fake_start_loop, raising=True)
    monkeypatch.setattr(NodeClient, "stop_loop", fake_stop_loop, raising=True)
    monkeypatch.setattr(NodeClient, "get_loop", fake_get_loop, raising=True)

    # ── Build the app ───────────────────────────────────────────────────────
    from gencall.controller.app import create_controller_app
    app, config = create_controller_app(Config())

    # The aggregator runs a background poll thread; for deterministic tests we
    # stop it and drive node_status / get_fleet_stats from `fleet` directly.
    from gencall.controller import routes as controller_routes
    aggregator = controller_routes.aggregator

    def fake_node_status(node_id):
        spec = fleet.get(node_id)
        if spec is None:
            return None
        return {"node_id": node_id, "online": bool(spec.get("online")),
                "version": "2.0.0", "active_tests": spec.get("active_tests", 0),
                "last_seen": None, "error": None,
                "group_id": spec.get("group_id")}

    def fake_is_online(node_id):
        return bool(fleet.get(node_id, {}).get("online", False))

    def fake_get_fleet_stats():
        from gencall.controller.aggregator import (
            aggregate_snapshots, empty_snapshot,
        )
        per_node = {}
        per_group_lists: dict = {}
        online_snaps = []
        for nid, spec in fleet.items():
            if spec.get("online") and spec.get("stats"):
                snap = dict(spec["stats"])
                per_node[nid] = snap
                online_snaps.append(snap)
                gid = spec.get("group_id")
                if gid is not None:
                    per_group_lists.setdefault(gid, []).append(snap)
            else:
                per_node[nid] = None
        per_group = {gid: aggregate_snapshots(s)
                     for gid, s in per_group_lists.items()}
        agg = aggregate_snapshots(online_snaps) if online_snaps else empty_snapshot()
        return {"aggregate": agg, "per_group": per_group, "per_node": per_node}

    monkeypatch.setattr(aggregator, "node_status", fake_node_status, raising=True)
    monkeypatch.setattr(aggregator, "is_online", fake_is_online, raising=True)
    monkeypatch.setattr(aggregator, "get_fleet_stats", fake_get_fleet_stats,
                        raising=True)
    # Never actually launch the poll thread during tests.
    monkeypatch.setattr(aggregator, "start", lambda: None, raising=True)
    monkeypatch.setattr(aggregator, "stop", lambda: None, raising=True)

    # Mint a known admin key in the controller DB so we can authenticate.
    from gencall.api import routes as worker_routes
    raw_key, _ = worker_routes.gateway.keys.create_key("test-admin")
    headers = {"X-API-Key": raw_key}

    class Ctx:
        pass

    ctx = Ctx()
    ctx.fleet = fleet
    ctx.dispatched_calls = dispatched_calls
    ctx.loop_dispatched_calls = loop_dispatched_calls

    def register_node(client, nid_address, group_id=None, online=False, stats=None,
                      start_raises=False, loop_stats=None, **node_fields):
        """Create a node via the API and register its mocked state in `fleet`."""
        body = {"name": node_fields.get("name", f"node-{nid_address}"),
                "address": nid_address,
                "group_id": group_id,
                "api_key": node_fields.get("api_key", "k"),
                "enabled": node_fields.get("enabled", True)}
        resp = client.post("/api/nodes", json=body, headers=headers)
        assert resp.status_code == 200, resp.text
        node = resp.json()
        fleet[node["id"]] = {"address": nid_address, "group_id": group_id,
                             "online": online, "stats": stats,
                             "start_raises": start_raises,
                             "loop_stats": loop_stats}
        return node

    ctx.register_node = register_node

    with TestClient(app) as client:
        yield client, headers, ctx

    Config.reset()
    try:
        os.unlink(tmp.name)
    except OSError:
        pass


# ─── split_rate helper ───────────────────────────────────────────────────────


def test_split_rate_per_node_gives_each_node_full_value():
    from gencall.controller.aggregator import split_rate
    assert split_rate("per_node", 5.0, 3) == [5.0, 5.0, 5.0]
    # Default mode is per_node.
    assert split_rate("", 2.0, 2) == [2.0, 2.0]
    assert split_rate("per_node", 1.0, 0) == []


def test_split_rate_total_splits_evenly():
    from gencall.controller.aggregator import split_rate
    assert split_rate("total", 9.0, 3) == [3.0, 3.0, 3.0]


def test_split_rate_total_distributes_remainder_to_first_nodes():
    from gencall.controller.aggregator import split_rate
    # 10 cps across 3 nodes -> 4,3,3 (remainder of 0.01-units to the front).
    rates = split_rate("total", 10.0, 3)
    assert rates == [3.34, 3.33, 3.33]
    assert round(sum(rates), 2) == 10.0
    # Integer-clean remainder: 7 across 2 -> 3.5 / 3.5.
    assert split_rate("total", 7.0, 2) == [3.5, 3.5]
    # No targets -> empty.
    assert split_rate("total", 10.0, 0) == []


# ─── Auth ────────────────────────────────────────────────────────────────────


PROTECTED = [
    ("get", "/api/nodes"),
    ("post", "/api/nodes"),
    ("get", "/api/groups"),
    ("post", "/api/groups"),
    ("post", "/api/fleet/launch"),
    ("post", "/api/fleet/loops/launch"),
    ("get", "/api/fleet/runs"),
    ("get", "/api/fleet/stats"),
    ("get", "/api/fleet/stats/history"),
]


@pytest.mark.parametrize("method,path", PROTECTED)
def test_endpoints_require_api_key(controller, method, path):
    client, _headers, _ctx = controller
    if method == "get":
        resp = client.get(path)
    else:
        resp = getattr(client, method)(path, json={})
    assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}"


def test_health_is_unauthenticated(controller):
    client, _headers, _ctx = controller
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["mode"] == "controller"


def test_valid_key_is_accepted(controller):
    client, headers, _ctx = controller
    assert client.get("/api/nodes", headers=headers).status_code == 200


# ─── Node CRUD ───────────────────────────────────────────────────────────────


def test_node_crud_lifecycle(controller):
    client, headers, _ctx = controller

    # Empty to start.
    resp = client.get("/api/nodes", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["nodes"] == []

    # Create.
    resp = client.post("/api/nodes", json={
        "name": "edge-1", "address": "http://node-1/", "api_key": "k1",
    }, headers=headers)
    assert resp.status_code == 200
    node = resp.json()
    nid = node["id"]
    assert node["name"] == "edge-1"
    # Trailing slash is stripped by the route.
    assert node["address"] == "http://node-1"
    assert node["enabled"] is True

    # List shows it.
    listing = client.get("/api/nodes", headers=headers).json()["nodes"]
    assert [n["id"] for n in listing] == [nid]

    # Update.
    resp = client.put(f"/api/nodes/{nid}", json={"name": "edge-1b",
                                                 "enabled": False},
                      headers=headers)
    assert resp.status_code == 200
    updated = resp.json()
    assert updated["name"] == "edge-1b"
    assert updated["enabled"] is False

    # Delete.
    resp = client.delete(f"/api/nodes/{nid}", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"status": "deleted", "id": nid}
    assert client.get("/api/nodes", headers=headers).json()["nodes"] == []


def test_node_update_and_delete_404(controller):
    client, headers, _ctx = controller
    assert client.put("/api/nodes/999", json={"name": "x"},
                      headers=headers).status_code == 404
    assert client.delete("/api/nodes/999", headers=headers).status_code == 404


def test_node_check_uses_mocked_health(controller):
    client, headers, ctx = controller
    node = ctx.register_node(client, "http://node-1", online=True)
    resp = client.post(f"/api/nodes/{node['id']}/check", headers=headers)
    assert resp.status_code == 200
    view = resp.json()
    assert view["online"] is True
    assert view["version"] == "2.0.0"

    # Flip the mocked node offline -> check reports offline with an error.
    ctx.fleet[node["id"]]["online"] = False
    resp = client.post(f"/api/nodes/{node['id']}/check", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["online"] is False


# ─── Group CRUD + membership ─────────────────────────────────────────────────


def test_group_crud_and_membership(controller):
    client, headers, ctx = controller

    # Create a group.
    resp = client.post("/api/groups", json={"name": "us-east",
                                            "description": "east coast"},
                       headers=headers)
    assert resp.status_code == 200
    group = resp.json()
    gid = group["id"]
    assert group["name"] == "us-east"
    assert group["node_ids"] == []
    assert group["total_count"] == 0

    # Two nodes, initially ungrouped.
    n1 = ctx.register_node(client, "http://node-1", online=True)
    n2 = ctx.register_node(client, "http://node-2", online=False)

    # Assign both via membership update.
    resp = client.put(f"/api/groups/{gid}",
                      json={"node_ids": [n1["id"], n2["id"]]}, headers=headers)
    assert resp.status_code == 200
    view = resp.json()
    assert set(view["node_ids"]) == {n1["id"], n2["id"]}
    assert view["total_count"] == 2
    # Mocked aggregator: only n1 is online.
    assert view["online_count"] == 1

    # Drop n2 from the group -> it is orphaned (group_id cleared), not deleted.
    resp = client.put(f"/api/groups/{gid}", json={"node_ids": [n1["id"]]},
                      headers=headers)
    assert resp.status_code == 200
    assert resp.json()["node_ids"] == [n1["id"]]
    # n2 still exists as a node.
    all_ids = {n["id"] for n in client.get("/api/nodes",
                                           headers=headers).json()["nodes"]}
    assert n2["id"] in all_ids

    # Rename + describe.
    resp = client.put(f"/api/groups/{gid}",
                      json={"name": "us-west", "description": "moved"},
                      headers=headers)
    assert resp.json()["name"] == "us-west"
    assert resp.json()["description"] == "moved"

    # List.
    groups = client.get("/api/groups", headers=headers).json()["groups"]
    assert [g["id"] for g in groups] == [gid]

    # Delete the group -> members orphaned, group gone.
    resp = client.delete(f"/api/groups/{gid}", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"status": "deleted", "id": gid}
    assert client.get("/api/groups", headers=headers).json()["groups"] == []
    # Node n1 survived deletion of its group.
    survivors = {n["id"] for n in client.get("/api/nodes",
                                             headers=headers).json()["nodes"]}
    assert {n1["id"], n2["id"]} <= survivors


def test_group_update_and_delete_404(controller):
    client, headers, _ctx = controller
    assert client.put("/api/groups/999", json={"name": "x"},
                      headers=headers).status_code == 404
    assert client.delete("/api/groups/999", headers=headers).status_code == 404


# ─── Fleet launch fan-out (partial failure) ──────────────────────────────────


def test_fleet_launch_partial_failure(controller):
    client, headers, ctx = controller

    # Three nodes: n1 online & OK, n2 online but start_test raises, n3 offline.
    n1 = ctx.register_node(client, "http://node-1", online=True)
    n2 = ctx.register_node(client, "http://node-2", online=True,
                           start_raises=True)
    n3 = ctx.register_node(client, "http://node-3", online=False)

    resp = client.post("/api/fleet/launch", json={
        "name": "campaign-A",
        "node_ids": [n1["id"], n2["id"], n3["id"]],
        "scenario": "basic_call",
        "destination": {"remote_host": "10.0.0.9", "remote_port": 5060,
                        "transport": "udp"},
        "rate": {"mode": "per_node", "value": 4.0},
    }, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    run_id = body["fleet_run_id"]

    by_node = {d["node_id"]: d for d in body["dispatched"]}
    # n1 dispatched OK with a test id.
    assert by_node[n1["id"]]["ok"] is True
    assert by_node[n1["id"]]["test_id"] == f"test-{n1['id']}"
    # n2 online but worker rejected -> error recorded.
    assert by_node[n2["id"]]["ok"] is False
    assert by_node[n2["id"]]["error"]
    # n3 offline -> never dispatched, marked offline.
    assert by_node[n3["id"]]["ok"] is False
    assert by_node[n3["id"]]["error"] == "node offline"

    # Only ONLINE targets get a start_test call; offline node was skipped.
    called_ids = {c["node_id"] for c in ctx.dispatched_calls}
    assert called_ids == {n1["id"], n2["id"]}
    # per_node mode -> each online node got the full value.
    for call in ctx.dispatched_calls:
        assert call["payload"]["call_rate"] == 4.0

    # Mixed ok/error -> run status 'partial', persisted on the run record.
    run = client.get(f"/api/fleet/runs/{run_id}", headers=headers).json()
    assert run["status"] == "partial"
    assert len(run["results"]) == 3


def test_fleet_launch_total_rate_split(controller):
    client, headers, ctx = controller
    n1 = ctx.register_node(client, "http://node-1", online=True)
    n2 = ctx.register_node(client, "http://node-2", online=True)

    resp = client.post("/api/fleet/launch", json={
        "node_ids": [n1["id"], n2["id"]],
        "scenario": "basic_call",
        "destination": {"remote_host": "10.0.0.9"},
        "rate": {"mode": "total", "value": 10.0},
    }, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["dispatched"]  # both dispatched

    rates = sorted(c["payload"]["call_rate"] for c in ctx.dispatched_calls)
    assert rates == [5.0, 5.0]


def test_fleet_launch_requires_targets(controller):
    client, headers, _ctx = controller
    resp = client.post("/api/fleet/launch", json={
        "scenario": "basic_call",
        "destination": {"remote_host": "10.0.0.9"},
    }, headers=headers)
    assert resp.status_code == 400


# ─── Fleet stats aggregation ─────────────────────────────────────────────────


def _snap(total, ok, failed, current, cps, rt):
    return {
        "timestamp": 1.0,
        "active_instances": 1,
        "total_calls": total,
        "successful_calls": ok,
        "failed_calls": failed,
        "current_calls": current,
        "calls_per_second": cps,
        "avg_response_time_ms": rt,
        "success_rate": round((ok / (ok + failed) * 100) if (ok + failed) else 0, 2),
    }


def test_fleet_stats_sums_across_nodes(controller):
    client, headers, ctx = controller

    grp = client.post("/api/groups", json={"name": "g1"},
                      headers=headers).json()
    gid = grp["id"]

    # Two online nodes in the group with concrete snapshots, one offline node.
    n1 = ctx.register_node(client, "http://node-1", group_id=gid, online=True,
                           stats=_snap(100, 80, 20, 5, 10.0, 50.0))
    n2 = ctx.register_node(client, "http://node-2", group_id=gid, online=True,
                           stats=_snap(40, 30, 10, 2, 4.0, 100.0))
    n3 = ctx.register_node(client, "http://node-3", online=False,
                           stats=_snap(999, 999, 0, 99, 99.0, 999.0))

    resp = client.get("/api/fleet/stats", headers=headers)
    assert resp.status_code == 200
    stats = resp.json()
    agg = stats["aggregate"]

    # Sums across the two ONLINE nodes only (offline n3 excluded).
    assert agg["total_calls"] == 140
    assert agg["successful_calls"] == 110
    assert agg["failed_calls"] == 30
    assert agg["current_calls"] == 7
    assert agg["calls_per_second"] == 14.0
    assert agg["active_instances"] == 2
    # Response time averaged over contributing nodes: (50 + 100) / 2.
    assert agg["avg_response_time_ms"] == 75.0
    # success_rate recomputed from totals: 110 / 140 * 100.
    assert agg["success_rate"] == round(110 / 140 * 100, 2)

    # per_node: online nodes carry a snapshot, offline node is null.
    per_node = stats["per_node"]
    assert per_node[str(n1["id"])]["total_calls"] == 100
    assert per_node[str(n2["id"])]["total_calls"] == 40
    assert per_node[str(n3["id"])] is None

    # per_group rollup for the group sums its two online members.
    per_group = stats["per_group"]
    assert per_group[str(gid)]["total_calls"] == 140


def test_fleet_stats_empty_when_no_online_nodes(controller):
    client, headers, ctx = controller
    ctx.register_node(client, "http://node-1", online=False)
    stats = client.get("/api/fleet/stats", headers=headers).json()
    assert stats["aggregate"]["total_calls"] == 0
    assert stats["aggregate"]["success_rate"] == 0.0


# ─── Fleet LOOP-campaign launch fan-out (design §4.4 / §7 stage 9) ─────────────


def test_fleet_loop_launch_fans_out_per_node(controller):
    client, headers, ctx = controller

    # Three nodes: n1 online & OK, n2 online but start_loop raises, n3 offline.
    n1 = ctx.register_node(client, "http://node-1", online=True)
    n2 = ctx.register_node(client, "http://node-2", online=True,
                           start_raises=True)
    n3 = ctx.register_node(client, "http://node-3", online=False)

    resp = client.post("/api/fleet/loops/launch", json={
        "name": "loop-A",
        "node_ids": [n1["id"], n2["id"], n3["id"]],
        "destination": {"remote_host": "10.0.0.9", "remote_port": 5060,
                        "transport": "udp"},
        "rate": {"mode": "per_node", "value": 2.0},
        "csv_path": "/data/pairs.csv",
        "max_concurrent": 20,
        "duration_s": 120,
        "target_minutes": 600,
    }, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    run_id = body["fleet_run_id"]

    by_node = {d["node_id"]: d for d in body["dispatched"]}
    # n1 dispatched OK with a campaign id.
    assert by_node[n1["id"]]["ok"] is True
    assert by_node[n1["id"]]["campaign_id"] == f"camp-{n1['id']}"
    # n2 online but worker rejected -> error recorded, no campaign id.
    assert by_node[n2["id"]]["ok"] is False
    assert by_node[n2["id"]]["error"]
    assert by_node[n2["id"]]["campaign_id"] is None
    # n3 offline -> never dispatched, marked offline.
    assert by_node[n3["id"]]["ok"] is False
    assert by_node[n3["id"]]["error"] == "node offline"

    # Only ONLINE targets get a start_loop call (POST /api/loops per node);
    # offline node was skipped.
    started = [c for c in ctx.loop_dispatched_calls if "payload" in c]
    called_ids = {c["node_id"] for c in started}
    assert called_ids == {n1["id"], n2["id"]}
    # Per-call loop params propagate onto the StartLoopRequest body, and
    # per_node mode -> each online node got the full rate value.
    for call in started:
        assert call["payload"]["rate"] == 2.0
        assert call["payload"]["csv_path"] == "/data/pairs.csv"
        assert call["payload"]["max_concurrent"] == 20
        assert call["payload"]["dest_host"] == "10.0.0.9"
        assert call["payload"]["target_minutes"] == 600

    # Mixed ok/error -> run status 'partial', persisted as a FleetRun.
    run = client.get(f"/api/fleet/runs/{run_id}", headers=headers).json()
    assert run["status"] == "partial"
    assert run["scenario"] == "__loop__"
    assert set(run["node_ids"]) == {n1["id"], n2["id"], n3["id"]}
    assert len(run["results"]) == 3


def test_fleet_loop_launch_total_rate_split(controller):
    client, headers, ctx = controller
    n1 = ctx.register_node(client, "http://node-1", online=True)
    n2 = ctx.register_node(client, "http://node-2", online=True)

    resp = client.post("/api/fleet/loops/launch", json={
        "node_ids": [n1["id"], n2["id"]],
        "destination": {"remote_host": "10.0.0.9"},
        "rate": {"mode": "total", "value": 10.0},
    }, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["dispatched"]

    started = [c for c in ctx.loop_dispatched_calls if "payload" in c]
    rates = sorted(c["payload"]["rate"] for c in started)
    assert rates == [5.0, 5.0]


def test_fleet_loop_launch_requires_targets(controller):
    client, headers, _ctx = controller
    resp = client.post("/api/fleet/loops/launch", json={
        "destination": {"remote_host": "10.0.0.9"},
    }, headers=headers)
    assert resp.status_code == 400


def test_fleet_loop_stop_calls_each_node(controller):
    client, headers, ctx = controller
    n1 = ctx.register_node(client, "http://node-1", online=True)
    n2 = ctx.register_node(client, "http://node-2", online=True)

    launch = client.post("/api/fleet/loops/launch", json={
        "node_ids": [n1["id"], n2["id"]],
        "destination": {"remote_host": "10.0.0.9"},
        "rate": {"mode": "per_node", "value": 1.0},
    }, headers=headers).json()
    run_id = launch["fleet_run_id"]

    resp = client.post(f"/api/fleet/loops/{run_id}/stop", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"status": "stopped"}

    # Each running campaign's stop endpoint was called per node.
    stopped = {c["stop_campaign"] for c in ctx.loop_dispatched_calls
               if "stop_campaign" in c}
    assert stopped == {f"camp-{n1['id']}", f"camp-{n2['id']}"}

    run = client.get(f"/api/fleet/runs/{run_id}", headers=headers).json()
    assert run["status"] == "stopped"


def test_fleet_loop_stop_404(controller):
    client, headers, _ctx = controller
    assert client.post("/api/fleet/loops/999/stop",
                       headers=headers).status_code == 404


# ─── Fleet loop_stats aggregation across nodes ────────────────────────────────


def _loop_stats(calls_out, answered_out, out_ms, calls_in, in_ms,
                completion, delta_avg, failures=None):
    return {
        "calls_out": calls_out,
        "answered_out": answered_out,
        "minutes_out_ms": out_ms,
        "calls_in_matched": calls_in,
        "minutes_in_ms": in_ms,
        "completion_pct": completion,
        "delta_avg_ms": delta_avg,
        "delta_p50_ms": delta_avg,
        "delta_p95_ms": delta_avg,
        "failures": failures or {"out": {}, "in": {}},
    }


def test_fleet_loop_view_sums_loop_stats_across_nodes(controller):
    client, headers, ctx = controller

    # Two online nodes, each running a loop with its own loop_stats snapshot.
    n1 = ctx.register_node(
        client, "http://node-1", online=True,
        loop_stats=_loop_stats(
            calls_out=100, answered_out=90, out_ms=600000,
            calls_in=80, in_ms=660000, completion=80.0, delta_avg=200.0,
            failures={"out": {"503": 5}, "in": {"487": 2}}))
    n2 = ctx.register_node(
        client, "http://node-2", online=True,
        loop_stats=_loop_stats(
            calls_out=40, answered_out=38, out_ms=240000,
            calls_in=30, in_ms=246000, completion=75.0, delta_avg=300.0,
            failures={"out": {"503": 1}, "in": {"480": 4}}))

    launch = client.post("/api/fleet/loops/launch", json={
        "node_ids": [n1["id"], n2["id"]],
        "destination": {"remote_host": "10.0.0.9"},
        "rate": {"mode": "per_node", "value": 1.0},
    }, headers=headers).json()
    run_id = launch["fleet_run_id"]

    resp = client.get(f"/api/fleet/loops/{run_id}", headers=headers)
    assert resp.status_code == 200
    view = resp.json()
    agg = view["aggregate"]

    # Counters summed across both nodes.
    assert agg["calls_out"] == 140
    assert agg["answered_out"] == 128
    assert agg["minutes_out_ms"] == 840000
    assert agg["calls_in_matched"] == 110
    assert agg["minutes_in_ms"] == 906000
    # Minutes derived from summed ms.
    assert agg["minutes_out"] == 14.0
    assert agg["minutes_in"] == 15.1
    # Completion recomputed from summed totals: 110 / 140 * 100 (NOT the mean of
    # 80 and 75).
    assert agg["completion_pct"] == round(110 / 140 * 100, 2)
    # delta_avg_ms averaged over contributing nodes: (200 + 300) / 2.
    assert agg["delta_avg_ms"] == 250.0
    # Failures merged per SIP code across nodes and directions.
    assert agg["failures"]["out"] == {"503": 6}
    assert agg["failures"]["in"] == {"487": 2, "480": 4}
    assert agg["nodes_contributing"] == 2

    # per_node echo carries each node's raw snapshot.
    per_node = view["per_node"]
    assert per_node[str(n1["id"])]["calls_out"] == 100
    assert per_node[str(n2["id"])]["calls_out"] == 40


def test_fleet_loop_view_404(controller):
    client, headers, _ctx = controller
    assert client.get("/api/fleet/loops/999",
                      headers=headers).status_code == 404


# ─── FleetSettings singleton (controller-managed trust whitelist) ─────────────


def test_fleet_settings_singleton_get_set(tmp_path):
    from gencall.controller.models import ControllerDatabase
    db = ControllerDatabase(f"sqlite:///{tmp_path / 'ctl.db'}")
    db.create_tables()
    # default
    assert db.get_fleet_trust() == {"enabled": False, "ips": [], "drop_untrusted": False}
    db.set_fleet_trust(enabled=True, ips=["10.0.0.1", "10.0.0.2"], drop_untrusted=True)
    assert db.get_fleet_trust() == {"enabled": True, "ips": ["10.0.0.1", "10.0.0.2"], "drop_untrusted": True}
    # singleton: a second set updates the same row, not a new one
    db.set_fleet_trust(enabled=False, ips=[], drop_untrusted=False)
    assert db.get_fleet_trust()["enabled"] is False


def test_effective_fleet_ips_gates_on_enabled(tmp_path):
    from gencall.controller.models import ControllerDatabase
    db = ControllerDatabase(f"sqlite:///{tmp_path / 'ctl2.db'}")
    db.create_tables()
    db.set_fleet_trust(enabled=False, ips=["10.0.0.1"], drop_untrusted=False)
    # Disabled keeps the saved list (for the UI) but enforces an empty (allow-all) list.
    assert db.get_fleet_trust()["ips"] == ["10.0.0.1"]
    assert db.effective_fleet_ips() == []
    db.set_fleet_trust(enabled=True, ips=["10.0.0.1"], drop_untrusted=False)
    assert db.effective_fleet_ips() == ["10.0.0.1"]


# ─── Controller fleet trust fan-out endpoint ──────────────────────────────────


def test_fleet_trust_endpoint_saves_and_fans_out(controller, monkeypatch):
    """POST /api/fleet/config/trust persists the singleton and pushes the
    EFFECTIVE list to every enabled worker via NodeClient.set_trust_whitelist."""
    client, headers, ctx = controller

    pushed = []

    async def fake_push(self, ips, drop_untrusted):
        pushed.append((self.address, list(ips), drop_untrusted))
        return {"status": "applied", "ips": ips, "drop_untrusted": drop_untrusted}

    from gencall.controller.node_client import NodeClient
    monkeypatch.setattr(NodeClient, "set_trust_whitelist", fake_push, raising=True)

    # Two enabled workers (online state is irrelevant — push targets ENABLED).
    ctx.register_node(client, "http://node-1", online=True)
    ctx.register_node(client, "http://node-2", online=False)

    r = client.post("/api/fleet/config/trust",
                    json={"enabled": True, "ips": ["10.0.0.1"], "drop_untrusted": True},
                    headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is True
    assert body["ips"] == ["10.0.0.1"]
    assert body["pushed"] == 2
    addrs = {res["address"] for res in body["results"] if res["ok"]}
    assert addrs == {"http://node-1", "http://node-2"}
    assert ("http://node-1", ["10.0.0.1"], True) in pushed
    assert ("http://node-2", ["10.0.0.1"], True) in pushed

    # Persisted on the controller singleton, and GET reflects it.
    from gencall.controller import routes as controller_routes
    assert controller_routes.db.get_fleet_trust()["ips"] == ["10.0.0.1"]
    got = client.get("/api/fleet/config/trust", headers=headers).json()
    assert got == {"enabled": True, "ips": ["10.0.0.1"], "drop_untrusted": True}


def test_fleet_trust_disabled_pushes_empty_but_keeps_saved(controller, monkeypatch):
    """enabled=false pushes an empty (allow-all) list to workers but keeps the
    saved list for the UI."""
    client, headers, ctx = controller

    pushed = []

    async def fake_push(self, ips, drop_untrusted):
        pushed.append((self.address, list(ips), drop_untrusted))
        return {"status": "applied"}

    from gencall.controller.node_client import NodeClient
    monkeypatch.setattr(NodeClient, "set_trust_whitelist", fake_push, raising=True)

    ctx.register_node(client, "http://node-1", online=True)

    r = client.post("/api/fleet/config/trust",
                    json={"enabled": False, "ips": ["10.0.0.1", "10.0.0.2"],
                          "drop_untrusted": False},
                    headers=headers)
    assert r.status_code == 200, r.text
    # Saved list preserved for the UI...
    assert r.json()["ips"] == ["10.0.0.1", "10.0.0.2"]
    # ...but the EFFECTIVE push to the worker is empty (allow-all).
    assert pushed == [("http://node-1", [], False)]


def test_fleet_trust_endpoint_rejects_bad_ip(controller):
    client, headers, _ctx = controller
    r = client.post("/api/fleet/config/trust",
                    json={"enabled": True, "ips": ["nope"], "drop_untrusted": False},
                    headers=headers)
    assert r.status_code == 422


def test_fleet_trust_endpoints_require_api_key(controller):
    client, _headers, _ctx = controller
    assert client.get("/api/fleet/config/trust").status_code == 401
    assert client.post("/api/fleet/config/trust", json={}).status_code == 401


# ─── Re-push fleet trust on a worker (re)join ─────────────────────────────────


def test_aggregator_repushes_trust_on_rejoin(monkeypatch):
    """A node that transitions offline→online gets the effective fleet trust
    re-pushed once (workers may have restarted with an empty whitelist); a node
    that stays online is not re-pushed again."""
    import asyncio

    from gencall.controller.aggregator import FleetAggregator
    from gencall.controller.node_client import NodeClient

    # Mutable: whether the single node's health probe succeeds this tick.
    online = {"v": False}
    pushed = []

    async def fake_health(self):
        if not online["v"]:
            raise RuntimeError("connection refused")
        return {"version": "2.0.0", "active_tests": 0, "status": "ok"}

    async def fake_push(self, ips, drop_untrusted):
        pushed.append((self.address, list(ips), drop_untrusted))
        return {"status": "applied"}

    monkeypatch.setattr(NodeClient, "health", fake_health, raising=True)
    monkeypatch.setattr(NodeClient, "set_trust_whitelist", fake_push, raising=True)

    nodes = [{"id": 1, "address": "http://w1", "api_key": "k1",
              "group_id": None, "enabled": True}]

    agg = FleetAggregator(
        lambda: nodes,
        fleet_trust_provider=lambda: {"ips": ["10.0.0.1"], "drop_untrusted": True},
    )

    # Tick 1: node is offline → no push.
    asyncio.run(agg._poll_health_once())
    assert pushed == []

    # Tick 2: node comes online (offline→online transition) → exactly one push.
    online["v"] = True
    asyncio.run(agg._poll_health_once())
    assert pushed == [("http://w1", ["10.0.0.1"], True)]

    # Tick 3: node stays online → no further push.
    asyncio.run(agg._poll_health_once())
    assert pushed == [("http://w1", ["10.0.0.1"], True)]


def test_aggregator_without_trust_provider_does_not_push(monkeypatch):
    """Backwards-compat: no fleet_trust_provider (default None) → never pushes,
    even on an offline→online transition."""
    import asyncio

    from gencall.controller.aggregator import FleetAggregator
    from gencall.controller.node_client import NodeClient

    pushed = []

    async def fake_health(self):
        return {"version": "2.0.0", "active_tests": 0, "status": "ok"}

    async def fake_push(self, ips, drop_untrusted):
        pushed.append(self.address)
        return {"status": "applied"}

    monkeypatch.setattr(NodeClient, "health", fake_health, raising=True)
    monkeypatch.setattr(NodeClient, "set_trust_whitelist", fake_push, raising=True)

    nodes = [{"id": 1, "address": "http://w1", "api_key": "k1",
              "group_id": None, "enabled": True}]
    agg = FleetAggregator(lambda: nodes)  # no provider
    asyncio.run(agg._poll_health_once())
    assert pushed == []
