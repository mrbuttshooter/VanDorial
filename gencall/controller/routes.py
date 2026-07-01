"""
VanDorial Fleet Controller — REST API (design §4).

Implements the controller API contract exactly:
  Nodes:      GET/POST /api/nodes, PUT/DELETE /api/nodes/{id}, POST .../check
  Groups:     GET/POST /api/groups, PUT/DELETE /api/groups/{id}
  Campaigns:  POST /api/fleet/launch, POST /api/fleet/{id}/stop,
              GET /api/fleet/runs, GET /api/fleet/runs/{id}
  Telemetry:  GET /api/fleet/stats, GET /api/fleet/stats/history
  Passthrough: ALL /api/nodes/{id}/proxy/{rest_of_path}
  System:     GET /api/health (UNAUTHENTICATED)

Every endpoint except /api/health is protected with the worker's auth dependency
`require_api_key` (design §6 — browser→controller reuses the exact same code
path). The dependency reads gencall.api.routes.gateway; app.py points that at a
controller APIGateway whose keys = APIKeyManager(db=<controller db>).
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Depends, Request, Response
from pydantic import BaseModel

# Reuse the worker's auth dependency verbatim (contract + design §6).
# `_reject_unsafe_worker_url` is the SSRF guard: the controller fetches/POSTs to
# a node's `address`, so block link-local/metadata targets there too.
from gencall.api.routes import require_api_key, _reject_unsafe_worker_url

from gencall.controller.models import Node, Group, FleetRun
from gencall.controller.node_client import NodeClient
from gencall.controller import ws as controller_ws

logger = logging.getLogger("gencall.controller.routes")

# Wired by app.create_controller_app().
db = None                # ControllerDatabase
aggregator = None        # FleetAggregator
verify_tls = False       # controller→node TLS verification toggle

router = APIRouter(tags=["controller"])


# ─── Pydantic request models ───────────────────────────────────────────────────

class NodeCreate(BaseModel):
    name: str
    address: str
    group_id: Optional[int] = None
    api_key: str = ""
    enabled: bool = True


class NodeUpdate(BaseModel):
    name: Optional[str] = None
    address: Optional[str] = None
    group_id: Optional[int] = None
    api_key: Optional[str] = None
    enabled: Optional[bool] = None


class GroupCreate(BaseModel):
    name: str
    description: str = ""


class GroupUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    node_ids: Optional[list[int]] = None


class Destination(BaseModel):
    remote_host: str
    remote_port: int = 5060
    transport: str = "udp"


class RateSpec(BaseModel):
    mode: str = "per_node"   # per_node | total
    value: float = 1.0


class AuthSpec(BaseModel):
    user: str = ""
    password: str = ""


class FleetLaunchRequest(BaseModel):
    name: str = ""
    group_id: Optional[int] = None
    node_ids: Optional[list[int]] = None
    scenario: str = "basic_call"
    destination: Destination
    rate: RateSpec = RateSpec()
    call_limit: Optional[int] = None
    max_calls: Optional[int] = None
    duration: Optional[int] = None
    auth: Optional[AuthSpec] = None


class FleetLoopLaunchRequest(BaseModel):
    """Fan-out launch of a Loop Campaign (design §4.4 / §7 stage 9).

    Mirrors FleetLaunchRequest's target-resolution + rate-split contract, but
    fans out POST /api/loops (StartLoopRequest) to every ONLINE target instead of
    the one-shot /api/tests/start. `rate` is split exactly like a test launch
    (per_node | total). Per-call loop params map straight onto the worker's
    StartLoopRequest body.
    """
    name: str = ""
    group_id: Optional[int] = None
    node_ids: Optional[list[int]] = None
    destination: Destination
    rate: RateSpec = RateSpec()
    csv_path: str = ""
    max_concurrent: int = 10
    duration_mode: str = "fixed"       # fixed | range
    duration_s: int = 180
    duration_max_s: int = 0
    match_key: str = "exact"
    target_calls: int = 0
    target_minutes: int = 0


# Marker stored in FleetRun.scenario to distinguish loop-campaign runs from
# one-shot test runs (so the stop/aggregate paths dispatch to /api/loops).
LOOP_RUN_SCENARIO = "__loop__"


# ─── helpers ───────────────────────────────────────────────────────────────────

def _require_db():
    if db is None:
        raise HTTPException(500, "Controller database not configured")
    return db


def _group_name_map(session) -> dict:
    return {g.id: g.name for g in session.query(Group).all()}


def _node_view(node: Node, group_names: dict) -> dict:
    """Build the API NodeView, merging DB record with live aggregator status."""
    status = aggregator.node_status(node.id) if aggregator else None
    health = node.health()
    if status:
        online = bool(status.get("online"))
        version = status.get("version") or health.get("version")
        active_tests = status.get("active_tests")
        if active_tests is None:
            active_tests = health.get("active_tests")
        error = status.get("error") or health.get("error")
        last_seen = status.get("last_seen")
        if last_seen is not None:
            last_seen = datetime.datetime.utcfromtimestamp(last_seen).isoformat()
        else:
            last_seen = node.last_seen.isoformat() if node.last_seen else None
    else:
        online = node.online
        version = health.get("version")
        active_tests = health.get("active_tests")
        error = health.get("error")
        last_seen = node.last_seen.isoformat() if node.last_seen else None

    return {
        "id": node.id,
        "name": node.name,
        "address": node.address,
        "group_id": node.group_id,
        "group_name": group_names.get(node.group_id),
        "enabled": bool(node.enabled),
        "online": online,
        "last_seen": last_seen,
        "version": version,
        "active_tests": active_tests if active_tests is not None else 0,
        "error": error,
    }


def _group_view(group: Group, session) -> dict:
    node_ids = [n.id for n in session.query(Node).filter_by(group_id=group.id).all()]
    online_count = 0
    if aggregator:
        online_count = sum(1 for nid in node_ids if aggregator.is_online(nid))
    return {
        "id": group.id,
        "name": group.name,
        "description": group.description or "",
        "node_ids": node_ids,
        "online_count": online_count,
        "total_count": len(node_ids),
    }


def _node_client(node: Node) -> NodeClient:
    return NodeClient(node.address, node.api_key, verify=verify_tls)


class FleetTrustBody(BaseModel):
    enabled: bool = False
    ips: list[str] = []
    drop_untrusted: bool = False


@router.get("/api/fleet/config/trust", dependencies=[Depends(require_api_key)])
def get_fleet_trust():
    """The persisted fleet-wide inbound trust config (FleetSettings singleton)."""
    if db is None:
        raise HTTPException(503, "controller database not configured")
    return db.get_fleet_trust()


@router.post("/api/fleet/config/trust", dependencies=[Depends(require_api_key)])
async def set_fleet_trust(body: FleetTrustBody):
    """Persist the fleet trust config and push the EFFECTIVE list to every enabled
    node's /api/config/trust. ``enabled=false`` => allow-all pushed (empty list),
    but the saved list is kept for the UI. Control-plane only — never touches a
    call."""
    import ipaddress
    for tok in body.ips:
        try:
            ipaddress.ip_network((tok or "").strip(), strict=False)
        except ValueError:
            raise HTTPException(422, f"invalid IP/CIDR: {tok!r}")
    if db is None:
        raise HTTPException(503, "controller database not configured")
    ips = [t.strip() for t in body.ips if t.strip()]
    # Persist the full config (the list is kept even when disabled, for the UI).
    db.set_fleet_trust(bool(body.enabled), ips, bool(body.drop_untrusted))
    # The EFFECTIVE push: disabled => allow-all (empty list, no dropping).
    eff_ips = ips if body.enabled else []
    eff_drop = bool(body.drop_untrusted) if body.enabled else False

    session = db.get_session()
    try:
        targets = [(n.address, _node_client(n))
                   for n in session.query(Node).all() if n.enabled]
    finally:
        session.close()

    async def _push(address, client):
        try:
            await client.set_trust_whitelist(eff_ips, eff_drop)
            return {"address": address, "ok": True, "error": None}
        except Exception as e:  # node offline / refused — reported, not fatal
            return {"address": address, "ok": False, "error": str(e)}

    results = list(await asyncio.gather(*[_push(a, c) for a, c in targets]))
    pushed = sum(1 for r in results if r["ok"])
    saved = db.get_fleet_trust()
    return {"enabled": saved["enabled"], "ips": saved["ips"],
            "drop_untrusted": saved["drop_untrusted"], "pushed": pushed,
            "results": results}


async def _probe_health(node: Node) -> dict:
    """Probe a node's health and persist it. Returns the health dict (with
    derived 'online')."""
    client = _node_client(node)
    try:
        h = await client.health()
        result = {
            "online": True,
            "version": h.get("version"),
            "active_tests": h.get("active_tests", 0),
            "status": h.get("status", "ok"),
            "error": None,
        }
    except Exception as exc:
        result = {"online": False, "error": str(exc)}

    session = _require_db().get_session()
    try:
        row = session.query(Node).filter_by(id=node.id).first()
        if row:
            row.last_health = json.dumps(result)
            if result.get("online"):
                row.last_seen = datetime.datetime.utcnow()
            session.commit()
    finally:
        session.close()
    return result


# ─── Nodes ──────────────────────────────────────────────────────────────────────

@router.get("/api/nodes", dependencies=[Depends(require_api_key)])
def list_nodes():
    session = _require_db().get_session()
    try:
        names = _group_name_map(session)
        nodes = session.query(Node).all()
        return {"nodes": [_node_view(n, names) for n in nodes]}
    finally:
        session.close()


@router.post("/api/nodes", dependencies=[Depends(require_api_key)])
def create_node(req: NodeCreate):
    session = _require_db().get_session()
    try:
        address = req.address.rstrip("/")
        _reject_unsafe_worker_url(address)
        node = Node(
            name=req.name,
            address=address,
            group_id=req.group_id,
            api_key=req.api_key,
            enabled=req.enabled,
        )
        session.add(node)
        session.commit()
        session.refresh(node)
        return _node_view(node, _group_name_map(session))
    except HTTPException:
        session.rollback()
        raise
    except Exception as exc:
        session.rollback()
        raise HTTPException(400, str(exc))
    finally:
        session.close()


@router.put("/api/nodes/{node_id}", dependencies=[Depends(require_api_key)])
def update_node(node_id: int, req: NodeUpdate):
    session = _require_db().get_session()
    try:
        node = session.query(Node).filter_by(id=node_id).first()
        if not node:
            raise HTTPException(404, f"Node {node_id} not found")
        if req.name is not None:
            node.name = req.name
        if req.address is not None:
            address = req.address.rstrip("/")
            _reject_unsafe_worker_url(address)
            node.address = address
        if req.group_id is not None:
            node.group_id = req.group_id
        if req.api_key is not None:
            node.api_key = req.api_key
        if req.enabled is not None:
            node.enabled = req.enabled
        session.commit()
        session.refresh(node)
        return _node_view(node, _group_name_map(session))
    finally:
        session.close()


@router.delete("/api/nodes/{node_id}", dependencies=[Depends(require_api_key)])
def delete_node(node_id: int):
    session = _require_db().get_session()
    try:
        node = session.query(Node).filter_by(id=node_id).first()
        if not node:
            raise HTTPException(404, f"Node {node_id} not found")
        session.delete(node)
        session.commit()
        return {"status": "deleted", "id": node_id}
    finally:
        session.close()


@router.post("/api/nodes/{node_id}/check", dependencies=[Depends(require_api_key)])
async def check_node(node_id: int):
    """Force an immediate health probe and return the refreshed NodeView."""
    session = _require_db().get_session()
    try:
        node = session.query(Node).filter_by(id=node_id).first()
        if not node:
            raise HTTPException(404, f"Node {node_id} not found")
    finally:
        session.close()

    # _probe_health opens its own session and commits the refreshed health.
    await _probe_health(node)

    # Re-read in a FRESH session so we observe the just-committed health.
    session = _require_db().get_session()
    try:
        names = _group_name_map(session)
        node = session.query(Node).filter_by(id=node_id).first()
        return _node_view(node, names)
    finally:
        session.close()


# ─── Groups ─────────────────────────────────────────────────────────────────────

@router.get("/api/groups", dependencies=[Depends(require_api_key)])
def list_groups():
    session = _require_db().get_session()
    try:
        groups = session.query(Group).all()
        return {"groups": [_group_view(g, session) for g in groups]}
    finally:
        session.close()


@router.post("/api/groups", dependencies=[Depends(require_api_key)])
def create_group(req: GroupCreate):
    session = _require_db().get_session()
    try:
        group = Group(name=req.name, description=req.description)
        session.add(group)
        session.commit()
        session.refresh(group)
        return _group_view(group, session)
    except Exception as exc:
        session.rollback()
        raise HTTPException(400, str(exc))
    finally:
        session.close()


@router.put("/api/groups/{group_id}", dependencies=[Depends(require_api_key)])
def update_group(group_id: int, req: GroupUpdate):
    session = _require_db().get_session()
    try:
        group = session.query(Group).filter_by(id=group_id).first()
        if not group:
            raise HTTPException(404, f"Group {group_id} not found")
        if req.name is not None:
            group.name = req.name
        if req.description is not None:
            group.description = req.description
        if req.node_ids is not None:
            # Reassign membership: set listed nodes to this group, clear others
            # that currently point here but aren't in the new list.
            wanted = set(req.node_ids)
            for node in session.query(Node).all():
                if node.id in wanted:
                    node.group_id = group_id
                elif node.group_id == group_id:
                    node.group_id = None
        session.commit()
        session.refresh(group)
        return _group_view(group, session)
    finally:
        session.close()


@router.delete("/api/groups/{group_id}", dependencies=[Depends(require_api_key)])
def delete_group(group_id: int):
    session = _require_db().get_session()
    try:
        group = session.query(Group).filter_by(id=group_id).first()
        if not group:
            raise HTTPException(404, f"Group {group_id} not found")
        # Orphan members rather than cascade-delete the nodes.
        for node in session.query(Node).filter_by(group_id=group_id).all():
            node.group_id = None
        session.delete(group)
        session.commit()
        return {"status": "deleted", "id": group_id}
    finally:
        session.close()


# ─── Fleet campaigns ────────────────────────────────────────────────────────────

def _resolve_targets(session, req: FleetLaunchRequest) -> list[Node]:
    """Resolve launch targets: explicit node_ids OR group_id members."""
    if req.node_ids:
        nodes = session.query(Node).filter(Node.id.in_(req.node_ids)).all()
    elif req.group_id is not None:
        nodes = session.query(Node).filter_by(group_id=req.group_id).all()
    else:
        raise HTTPException(400, "Provide group_id or node_ids")
    return [n for n in nodes if n.enabled]


def _is_target_online(node: Node) -> bool:
    """Whether a node should be dispatched to. The aggregator is the live source
    of truth; if it has no status yet for this node (e.g. just added, or a
    manual /check ran before the next poll), fall back to the persisted health."""
    if aggregator is not None:
        status = aggregator.node_status(node.id)
        if status is not None:
            return bool(status.get("online"))
    return node.online


@router.post("/api/fleet/launch", dependencies=[Depends(require_api_key)])
async def fleet_launch(req: FleetLaunchRequest):
    """Resolve targets, compute per-node rate, fan out POST /api/tests/start to
    every ONLINE target in parallel, persist a FleetRun, return the dispatch."""
    from gencall.controller.aggregator import split_rate

    session = _require_db().get_session()
    try:
        targets = _resolve_targets(session, req)
        online_targets = [n for n in targets if _is_target_online(n)]
        offline_targets = [n for n in targets if not _is_target_online(n)]

        try:
            rates = split_rate(req.rate.mode, req.rate.value, len(online_targets))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        # Snapshot the data we need so we can use it after closing the session.
        plan = []
        for node, rate in zip(online_targets, rates):
            plan.append({
                "id": node.id, "address": node.address,
                "api_key": node.api_key, "rate": rate,
            })
        offline_ids = [n.id for n in offline_targets]
        target_ids = [n.id for n in targets]

        run = FleetRun(
            name=req.name or "",
            group_id=req.group_id,
            node_ids=json.dumps(target_ids),
            scenario=req.scenario,
            destination=json.dumps(req.destination.model_dump()),
            rate_mode=req.rate.mode,
            rate_value=str(req.rate.value),
            status="running",
            started_at=datetime.datetime.utcnow(),
            results="[]",
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        fleet_run_id = run.id
    finally:
        session.close()

    base_payload = {
        "scenario": req.scenario,
        "remote_host": req.destination.remote_host,
        "remote_port": req.destination.remote_port,
        "transport": req.destination.transport,
        "call_limit": req.call_limit if req.call_limit is not None else 10,
        "max_calls": req.max_calls if req.max_calls is not None else 0,
        "duration": req.duration if req.duration is not None else 0,
    }
    if req.auth:
        base_payload["auth_user"] = req.auth.user
        base_payload["auth_pass"] = req.auth.password

    async def start_on(node_plan):
        payload = dict(base_payload)
        payload["call_rate"] = node_plan["rate"]
        payload["name"] = f"fleet-{fleet_run_id}-n{node_plan['id']}"
        client = NodeClient(node_plan["address"], node_plan["api_key"],
                            verify=verify_tls)
        try:
            resp = await client.start_test(payload)
            return {"node_id": node_plan["id"], "ok": True,
                    "test_id": resp.get("id"), "error": None}
        except Exception as exc:
            return {"node_id": node_plan["id"], "ok": False,
                    "test_id": None, "error": str(exc)}

    dispatched = list(await asyncio.gather(*(start_on(p) for p in plan))) if plan else []
    for nid in offline_ids:
        dispatched.append({"node_id": nid, "ok": False,
                           "test_id": None, "error": "node offline"})

    # Compute run status.
    ok_count = sum(1 for d in dispatched if d["ok"])
    if ok_count == 0:
        status = "failed"
    elif ok_count < len(dispatched):
        status = "partial"
    else:
        status = "running"

    session = _require_db().get_session()
    try:
        run = session.query(FleetRun).filter_by(id=fleet_run_id).first()
        if run:
            run.results = json.dumps(dispatched)
            run.status = status
            if status in ("failed",):
                run.completed_at = datetime.datetime.utcnow()
            session.commit()
    finally:
        session.close()

    controller_ws.emit_fleet_event({
        "event": "launch", "fleet_run_id": fleet_run_id,
        "status": status, "dispatched": dispatched,
    })

    return {"fleet_run_id": fleet_run_id, "dispatched": dispatched}


@router.post("/api/fleet/{run_id}/stop", dependencies=[Depends(require_api_key)])
async def fleet_stop(run_id: int):
    """Best-effort stop of every member test in a fleet run."""
    session = _require_db().get_session()
    try:
        run = session.query(FleetRun).filter_by(id=run_id).first()
        if not run:
            raise HTTPException(404, f"Fleet run {run_id} not found")
        results = run.get_results()
        node_map = {n.id: (n.address, n.api_key) for n in session.query(Node).all()}
    finally:
        session.close()

    async def stop_on(entry):
        nid = entry.get("node_id")
        test_id = entry.get("test_id")
        if not entry.get("ok") or not test_id or nid not in node_map:
            return
        addr, key = node_map[nid]
        client = NodeClient(addr, key, verify=verify_tls)
        try:
            await client.stop_test(test_id)
        except Exception:
            logger.debug("stop failed for run %s node %s", run_id, nid,
                         exc_info=True)

    await asyncio.gather(*(stop_on(e) for e in results))

    session = _require_db().get_session()
    try:
        run = session.query(FleetRun).filter_by(id=run_id).first()
        if run:
            run.status = "stopped"
            run.completed_at = datetime.datetime.utcnow()
            session.commit()
    finally:
        session.close()

    controller_ws.emit_fleet_event({
        "event": "stop", "fleet_run_id": run_id, "status": "stopped"})
    return {"status": "stopped"}


# ─── Fleet loop campaigns (design §4.4 / §7 stage 9) ────────────────────────────

@router.post("/api/fleet/loops/launch", dependencies=[Depends(require_api_key)])
async def fleet_loops_launch(req: FleetLoopLaunchRequest):
    """Resolve targets, compute per-node rate, fan out POST /api/loops to every
    ONLINE target in parallel, persist a FleetRun, return the dispatch.

    Reuses the exact target-resolution + rate-splitting + FleetRun-persistence
    pattern as /api/fleet/launch; the only differences are the per-node payload
    (a StartLoopRequest) and that each result carries `campaign_id` rather than
    `test_id`.
    """
    from gencall.controller.aggregator import split_rate

    session = _require_db().get_session()
    try:
        targets = _resolve_targets(session, req)
        online_targets = [n for n in targets if _is_target_online(n)]
        offline_targets = [n for n in targets if not _is_target_online(n)]

        try:
            rates = split_rate(req.rate.mode, req.rate.value, len(online_targets))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        plan = []
        for node, rate in zip(online_targets, rates):
            plan.append({
                "id": node.id, "address": node.address,
                "api_key": node.api_key, "rate": rate,
            })
        offline_ids = [n.id for n in offline_targets]
        target_ids = [n.id for n in targets]

        run = FleetRun(
            name=req.name or "",
            group_id=req.group_id,
            node_ids=json.dumps(target_ids),
            scenario=LOOP_RUN_SCENARIO,
            destination=json.dumps(req.destination.model_dump()),
            rate_mode=req.rate.mode,
            rate_value=str(req.rate.value),
            status="running",
            started_at=datetime.datetime.utcnow(),
            results="[]",
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        fleet_run_id = run.id
    finally:
        session.close()

    base_payload = {
        "dest_host": req.destination.remote_host,
        "dest_port": req.destination.remote_port,
        "transport": req.destination.transport,
        "csv_path": req.csv_path,
        "max_concurrent": req.max_concurrent,
        "duration_mode": req.duration_mode,
        "duration_s": req.duration_s,
        "duration_max_s": req.duration_max_s,
        "match_key": req.match_key,
        "target_calls": req.target_calls,
        "target_minutes": req.target_minutes,
    }

    async def start_on(node_plan):
        payload = dict(base_payload)
        payload["rate"] = node_plan["rate"]
        payload["name"] = f"fleet-loop-{fleet_run_id}-n{node_plan['id']}"
        client = NodeClient(node_plan["address"], node_plan["api_key"],
                            verify=verify_tls)
        try:
            resp = await client.start_loop(payload)
            campaign = resp.get("campaign") or {}
            return {"node_id": node_plan["id"], "ok": True,
                    "campaign_id": campaign.get("id"), "error": None}
        except Exception as exc:
            return {"node_id": node_plan["id"], "ok": False,
                    "campaign_id": None, "error": str(exc)}

    dispatched = list(await asyncio.gather(*(start_on(p) for p in plan))) if plan else []
    for nid in offline_ids:
        dispatched.append({"node_id": nid, "ok": False,
                           "campaign_id": None, "error": "node offline"})

    ok_count = sum(1 for d in dispatched if d["ok"])
    if ok_count == 0:
        status = "failed"
    elif ok_count < len(dispatched):
        status = "partial"
    else:
        status = "running"

    session = _require_db().get_session()
    try:
        run = session.query(FleetRun).filter_by(id=fleet_run_id).first()
        if run:
            run.results = json.dumps(dispatched)
            run.status = status
            if status in ("failed",):
                run.completed_at = datetime.datetime.utcnow()
            session.commit()
    finally:
        session.close()

    controller_ws.emit_fleet_event({
        "event": "loop_launch", "fleet_run_id": fleet_run_id,
        "status": status, "dispatched": dispatched,
    })

    return {"fleet_run_id": fleet_run_id, "dispatched": dispatched}


@router.post("/api/fleet/loops/{run_id}/stop", dependencies=[Depends(require_api_key)])
async def fleet_loops_stop(run_id: int):
    """Best-effort stop of every member loop campaign in a fleet loop run."""
    session = _require_db().get_session()
    try:
        run = session.query(FleetRun).filter_by(id=run_id).first()
        if not run:
            raise HTTPException(404, f"Fleet run {run_id} not found")
        results = run.get_results()
        node_map = {n.id: (n.address, n.api_key) for n in session.query(Node).all()}
    finally:
        session.close()

    async def stop_on(entry):
        nid = entry.get("node_id")
        campaign_id = entry.get("campaign_id")
        if not entry.get("ok") or not campaign_id or nid not in node_map:
            return
        addr, key = node_map[nid]
        client = NodeClient(addr, key, verify=verify_tls)
        try:
            await client.stop_loop(campaign_id)
        except Exception:
            logger.debug("loop stop failed for run %s node %s", run_id, nid,
                         exc_info=True)

    await asyncio.gather(*(stop_on(e) for e in results))

    session = _require_db().get_session()
    try:
        run = session.query(FleetRun).filter_by(id=run_id).first()
        if run:
            run.status = "stopped"
            run.completed_at = datetime.datetime.utcnow()
            session.commit()
    finally:
        session.close()

    controller_ws.emit_fleet_event({
        "event": "loop_stop", "fleet_run_id": run_id, "status": "stopped"})
    return {"status": "stopped"}


@router.get("/api/fleet/loops/{run_id}", dependencies=[Depends(require_api_key)])
async def fleet_loop_view(run_id: int):
    """Combined loop_stats across all member nodes of a loop fleet run.

    Polls each member node's GET /api/loops/{campaign_id} for its latest
    loop_stats and sums minutes-out/in + completion across nodes (design §7
    stage 9). Per-node snapshots are echoed so the console can drill in.
    """
    from gencall.controller.aggregator import aggregate_loop_stats

    session = _require_db().get_session()
    try:
        run = session.query(FleetRun).filter_by(id=run_id).first()
        if not run:
            raise HTTPException(404, f"Fleet run {run_id} not found")
        run_view = run.to_dict()
        results = run.get_results()
        node_map = {n.id: (n.address, n.api_key) for n in session.query(Node).all()}
    finally:
        session.close()

    async def fetch(entry):
        nid = entry.get("node_id")
        campaign_id = entry.get("campaign_id")
        if not entry.get("ok") or not campaign_id or nid not in node_map:
            return nid, None
        addr, key = node_map[nid]
        client = NodeClient(addr, key, verify=verify_tls)
        try:
            campaign = await client.get_loop(campaign_id)
            return nid, campaign.get("loop_stats")
        except Exception:
            logger.debug("loop fetch failed for run %s node %s", run_id, nid,
                         exc_info=True)
            return nid, None

    pairs = await asyncio.gather(*(fetch(e) for e in results)) if results else []
    per_node = {nid: stats for nid, stats in pairs}

    return {
        "fleet_run_id": run_id,
        "status": run_view["status"],
        "aggregate": aggregate_loop_stats(per_node),
        "per_node": per_node,
    }


@router.get("/api/fleet/runs", dependencies=[Depends(require_api_key)])
def list_fleet_runs(limit: int = Query(default=50, ge=1, le=500)):
    session = _require_db().get_session()
    try:
        runs = (session.query(FleetRun)
                .order_by(FleetRun.id.desc()).limit(limit).all())
        return {"runs": [r.to_dict() for r in runs]}
    finally:
        session.close()


@router.get("/api/fleet/runs/{run_id}", dependencies=[Depends(require_api_key)])
def get_fleet_run(run_id: int):
    session = _require_db().get_session()
    try:
        run = session.query(FleetRun).filter_by(id=run_id).first()
        if not run:
            raise HTTPException(404, f"Fleet run {run_id} not found")
        return run.to_dict()
    finally:
        session.close()


# ─── Aggregated telemetry ───────────────────────────────────────────────────────

@router.get("/api/fleet/stats", dependencies=[Depends(require_api_key)])
def fleet_stats():
    if aggregator is None:
        return {"aggregate": _empty(), "per_group": {}, "per_node": {}}
    return aggregator.get_fleet_stats()


@router.get("/api/fleet/stats/history", dependencies=[Depends(require_api_key)])
def fleet_stats_history(limit: int = Query(default=240, ge=1, le=10000)):
    if aggregator is None:
        return {"history": []}
    return {"history": aggregator.get_history(limit)}


def _empty() -> dict:
    from gencall.controller.aggregator import empty_snapshot
    return empty_snapshot()


# ─── Fleet inbound trust whitelist ────────────────────────────────────────────
# GET/POST /api/fleet/config/trust is defined earlier in this module. A duplicate
# definition lived here and was removed (FastAPI used the first registration, so
# this copy was dead code — see git history).


# ─── Single-node passthrough proxy ──────────────────────────────────────────────

@router.api_route(
    "/api/nodes/{node_id}/proxy/{rest_of_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    dependencies=[Depends(require_api_key)],
)
async def proxy_to_node(node_id: int, rest_of_path: str, request: Request):
    """Proxy ALL methods to the node's /{rest_of_path}, injecting its key."""
    session = _require_db().get_session()
    try:
        node = session.query(Node).filter_by(id=node_id).first()
        if not node:
            raise HTTPException(404, f"Node {node_id} not found")
        address, api_key = node.address, node.api_key
    finally:
        session.close()

    # Body (JSON if present).
    body = None
    raw = await request.body()
    if raw:
        try:
            body = json.loads(raw)
        except ValueError:
            body = None

    params = dict(request.query_params)
    client = NodeClient(address, api_key, verify=verify_tls)
    target_path = "/" + rest_of_path
    try:
        resp = await client.proxy(request.method, target_path,
                                  json=body, params=params or None)
    except Exception as exc:
        raise HTTPException(502, f"Node {node_id} unreachable: {exc}")

    media_type = resp.headers.get("content-type", "application/json")
    return Response(content=resp.content, status_code=resp.status_code,
                    media_type=media_type)


# ─── System (unauthenticated) ───────────────────────────────────────────────────

@router.get("/api/health")
def health_check():
    """Controller health (unauthenticated, mirrors worker shape)."""
    node_count = 0
    if db is not None:
        session = db.get_session()
        try:
            node_count = session.query(Node).count()
        except Exception:
            node_count = 0
        finally:
            session.close()
    return {
        "status": "ok",
        "version": "2.0.0",
        "name": "GenCall Controller",
        "mode": "controller",
        "nodes": node_count,
    }
