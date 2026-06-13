"""
Fleet-node registry + proxy (worker-side one-GUI control plane).

This box (a worker that also serves the console) can register OTHER GenCall
worker boxes on the VLAN and proxy the GUI's worker-facing calls to them. So the
single GUI on .4 manages .3 (and any worker) without a separate controller
process: pick a box in the topbar switcher and every /api/servers, /api/loops,
/api/loop-presets, /api/sale-zones … call is forwarded to that box.

Endpoints (all behind the same X-API-Key auth as the rest of the worker API):
  GET/POST/PUT/DELETE /api/fleet-nodes                 registry CRUD
  GET                 /api/fleet-nodes/{id}/health     live probe
  *                   /api/fleet-nodes/{id}/proxy/{p}  forward to that box

The forward reuses NodeClient (a thin httpx wrapper) and presents the registered
worker's own API key, exactly like the controller's passthrough proxy.
"""

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from gencall.api.routes import require_api_key
from gencall.controller.node_client import NodeClient

logger = logging.getLogger("gencall.api.fleet")

router = APIRouter()

# Wired in main.py (create_app): the worker's Database. None => 503.
db = None
# controller→node TLS verification toggle (self-signed dev certs default off).
verify_tls = False


def _db():
    if db is None:
        raise HTTPException(503, "Database not configured on this worker")
    return db


class FleetNodeRequest(BaseModel):
    name: str
    address: str            # http://host:port
    api_key: str = ""
    enabled: bool = True


class CheckRequest(BaseModel):
    """Probe an address+key WITHOUT saving it (the 'Test connection' button)."""
    address: str
    api_key: str = ""


async def _probe(address: str, api_key: str) -> dict:
    """Best-effort health of a remote worker (short timeout)."""
    try:
        h = await NodeClient(address, api_key, verify=verify_tls, timeout=4.0).health()
        return {"online": True, "version": h.get("version"), "error": None}
    except Exception as e:  # transport / HTTP / timeout
        return {"online": False, "version": None, "error": str(e)}


@router.get("/api/fleet-nodes", dependencies=[Depends(require_api_key)])
async def list_fleet_nodes():
    """List registered boxes, each with a live online/version probe."""
    from gencall.db.models import FleetNode

    session = _db().get_session()
    try:
        rows = [(n.id, n.name, n.address, n.api_key, bool(n.enabled),
                 n.created_at.isoformat() if n.created_at else None)
                for n in session.query(FleetNode).order_by(FleetNode.id).all()]
    finally:
        session.close()

    probes = (await asyncio.gather(*(_probe(a, k) for (_i, _n, a, k, _e, _c) in rows))
              if rows else [])
    out = []
    for i, (nid, name, addr, _key, enabled, created) in enumerate(rows):
        p = probes[i]
        out.append({
            "id": nid, "name": name, "address": addr, "enabled": enabled,
            "online": p["online"], "version": p["version"], "error": p["error"],
            "created_at": created,
        })
    return {"nodes": out}


@router.post("/api/fleet-nodes/check", dependencies=[Depends(require_api_key)])
async def check_fleet_node(req: CheckRequest):
    """Test reachability of a box address+key before saving it. Returns
    {online, version, error}. Declared before the create route — distinct path."""
    addr = (req.address or "").strip().rstrip("/")
    if not addr:
        raise HTTPException(422, "address is required")
    if not addr.startswith(("http://", "https://")):
        addr = "http://" + addr
    return {"address": addr, **(await _probe(addr, req.api_key))}


@router.post("/api/fleet-nodes", dependencies=[Depends(require_api_key)])
def create_fleet_node(req: FleetNodeRequest):
    """Register a remote worker box."""
    from gencall.db.models import FleetNode

    name = (req.name or "").strip()
    address = (req.address or "").strip().rstrip("/")
    if not name or not address:
        raise HTTPException(422, "name and address are required")
    if not address.startswith(("http://", "https://")):
        address = "http://" + address
    session = _db().get_session()
    try:
        n = FleetNode(name=name, address=address, api_key=req.api_key,
                      enabled=req.enabled)
        session.add(n)
        session.commit()
        return {"status": "created", "node": n.to_dict()}
    except Exception as e:
        session.rollback()
        raise HTTPException(400, f"could not add box (duplicate name?): {e}")
    finally:
        session.close()


@router.put("/api/fleet-nodes/{node_id}", dependencies=[Depends(require_api_key)])
def update_fleet_node(node_id: int, req: FleetNodeRequest):
    """Update a box (name/address/key/enabled). Blank api_key keeps the stored one."""
    from gencall.db.models import FleetNode

    session = _db().get_session()
    try:
        n = session.query(FleetNode).filter_by(id=node_id).first()
        if not n:
            raise HTTPException(404, f"Fleet node {node_id} not found")
        n.name = (req.name or n.name).strip()
        addr = (req.address or "").strip().rstrip("/")
        if addr:
            n.address = addr if addr.startswith(("http://", "https://")) else "http://" + addr
        if req.api_key:
            n.api_key = req.api_key
        n.enabled = req.enabled
        session.commit()
        return {"status": "updated", "node": n.to_dict()}
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(400, str(e))
    finally:
        session.close()


@router.delete("/api/fleet-nodes/{node_id}", dependencies=[Depends(require_api_key)])
def delete_fleet_node(node_id: int):
    from gencall.db.models import FleetNode

    session = _db().get_session()
    try:
        n = session.query(FleetNode).filter_by(id=node_id).first()
        if not n:
            raise HTTPException(404, f"Fleet node {node_id} not found")
        session.delete(n)
        session.commit()
        return {"status": "deleted", "id": node_id}
    finally:
        session.close()


@router.get("/api/fleet-nodes/{node_id}/health", dependencies=[Depends(require_api_key)])
async def fleet_node_health(node_id: int):
    from gencall.db.models import FleetNode

    session = _db().get_session()
    try:
        n = session.query(FleetNode).filter_by(id=node_id).first()
        if not n:
            raise HTTPException(404, f"Fleet node {node_id} not found")
        addr, key = n.address, n.api_key
    finally:
        session.close()
    return {"id": node_id, **(await _probe(addr, key))}


@router.api_route(
    "/api/fleet-nodes/{node_id}/proxy/{rest_of_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    dependencies=[Depends(require_api_key)],
)
async def proxy_fleet_node(node_id: int, rest_of_path: str, request: Request):
    """Forward ALL methods to the registered box's /{rest_of_path}, with its key."""
    from gencall.db.models import FleetNode

    session = _db().get_session()
    try:
        n = session.query(FleetNode).filter_by(id=node_id).first()
        if not n:
            raise HTTPException(404, f"Fleet node {node_id} not found")
        address, api_key = n.address, n.api_key
    finally:
        session.close()

    body = None
    raw = await request.body()
    if raw:
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
    params = dict(request.query_params)

    client = NodeClient(address, api_key, verify=verify_tls)
    try:
        resp = await client.proxy(request.method, "/" + rest_of_path,
                                  json=body, params=params or None)
    except Exception as e:
        raise HTTPException(502, f"Box {node_id} unreachable: {e}")

    return Response(content=resp.content, status_code=resp.status_code,
                    media_type=resp.headers.get("content-type", "application/json"))
