"""
GenCall REST API Routes.
FastAPI-based API for controlling the traffic generator.
"""

import os
import uuid
import datetime
import logging
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Header, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from gencall.core.sipp_engine import (
    SIPpEngine, SIPpInstance, SIPpMode, SIPpTransport, SIPpState
)
from gencall.core.stats import StatsEngine
from gencall.core.api_gateway import APIGateway
from gencall.scenarios.manager import ScenarioManager
from gencall.db.models import Database, Connector, Scenario, Server, TestRun, User

logger = logging.getLogger("gencall.api")

app = FastAPI(
    title="GenCall API",
    description="GenCall SIP Traffic Generator - REST API",
    version="2.0.0",
)

# These get set during app startup
engine: Optional[SIPpEngine] = None
stats: Optional[StatsEngine] = None
scenarios: Optional[ScenarioManager] = None
db: Optional[Database] = None
# API authentication gateway (keys + rate limiter + audit log). Wired in
# main.py when a database is available; None means auth is not configured.
gateway: Optional[APIGateway] = None


# ─── Authentication ──────────────────────────────────────────────────────────

def require_api_key(request: Request,
                    x_api_key: str = Header(default=None, alias="X-API-Key")):
    """FastAPI dependency enforcing a valid `X-API-Key` header.

    Validates the key against the persisted key store, applies the per-key rate
    limit, and records the call in the audit log.

    Fail CLOSED: if no gateway is wired (e.g. the database is unavailable so
    auth could not be configured) every protected endpoint is refused with 503
    rather than silently allowed. Previously this returned None (allow), which
    opened EVERY endpoint — including state-changing loop/fleet control — the
    moment the DB went away. Only the explicitly-unauthenticated read-only
    `/api/health` endpoints stay reachable, because they do not depend on this
    guard at all.
    """
    if gateway is None:
        raise HTTPException(503, "Authentication is not configured (no key store)")

    if not x_api_key:
        raise HTTPException(401, "Missing API key (set the X-API-Key header)")

    api_key = gateway.keys.validate_key(x_api_key)
    if not api_key:
        raise HTTPException(401, "Invalid or revoked API key")

    if not gateway.rate_limiter.check(api_key.key_id, api_key.rate_limit):
        raise HTTPException(429, "Rate limit exceeded")

    ip = request.client.host if request.client else ""
    gateway.audit.log(api_key, action=f"{request.method} {request.url.path}", ip=ip)
    return api_key


# ─── Pydantic Models ───────────────────────────────────────────────────────────

class StartTestRequest(BaseModel):
    name: str = ""
    scenario: str = "basic_call"
    remote_host: str
    remote_port: int = 5060
    local_ip: str = ""
    local_port: int = 5060
    transport: str = "udp"
    call_rate: float = 1.0
    max_calls: int = 0
    call_limit: int = 10
    duration: int = 0
    csv_file: str = ""
    auth_user: str = ""
    auth_pass: str = ""
    extra_args: str = ""


class UpdateRateRequest(BaseModel):
    call_rate: float


class ConnectorRequest(BaseModel):
    name: str
    description: str = ""
    local_ip: str
    local_port: int = 5060
    remote_ip: str
    remote_port: int = 5060
    transport: str = "udp"
    auth_user: str = ""
    auth_pass: str = ""


class ServerRequest(BaseModel):
    name: str
    ip: str
    description: str = ""
    group_id: Optional[int] = None  # optional NodeGroup membership
    # Optional per-node number pool. When origin_zone + dest_zone are given the
    # pool is generated on create; otherwise the node starts with no pool and you
    # generate it later via POST /api/servers/{id}/generate.
    origin_zone: str = ""
    dest_zone: str = ""
    # Optional pinned code within each zone (e.g. dial only 22462). Empty =>
    # spread across the whole zone's codes.
    origin_code: str = ""
    dest_code: str = ""
    # Pool size is bounded: generation is synchronous, so an unbounded count
    # could pin a request thread + RAM. 2M is plenty for a random-draw pool.
    count: int = Field(default=500000, ge=1, le=2_000_000)
    length: int = Field(default=11, ge=4, le=18)


class GeneratePoolRequest(BaseModel):
    """(Re)generate a node's number pool. Empty fields reuse the node's stored
    zones/codes/length so a bare POST just refreshes the pool."""
    origin_zone: str = ""
    dest_zone: str = ""
    origin_code: str = ""
    dest_code: str = ""
    count: int = Field(default=500000, ge=1, le=2_000_000)
    length: int = Field(default=0, ge=0, le=18)  # 0 => keep the node's stored length


class ScenarioRequest(BaseModel):
    name: str
    description: str = ""
    xml_content: str
    mode: str = "uac"


# ─── Test Control ──────────────────────────────────────────────────────────────

@app.post("/api/tests/start", dependencies=[Depends(require_api_key)])
def start_test(req: StartTestRequest):
    """Start a new SIP test."""
    scenario_path = scenarios.get_scenario_path(req.scenario)
    if not scenario_path:
        raise HTTPException(404, f"Scenario '{req.scenario}' not found")

    transport_map = {"udp": SIPpTransport.UDP, "tcp": SIPpTransport.TCP, "tls": SIPpTransport.TLS}
    transport = transport_map.get(req.transport.lower(), SIPpTransport.UDP)

    instance_id = req.name or f"test-{uuid.uuid4().hex[:8]}"

    instance = SIPpInstance(
        id=instance_id,
        scenario_file=scenario_path,
        remote_host=req.remote_host,
        remote_port=req.remote_port,
        local_port=req.local_port,
        local_ip=req.local_ip,
        transport=transport,
        call_rate=req.call_rate,
        max_calls=req.max_calls,
        call_limit=req.call_limit,
        duration=req.duration,
        csv_file=req.csv_file,
        auth_user=req.auth_user,
        auth_pass=req.auth_pass,
        extra_args=req.extra_args,
    )

    success = engine.start_instance(instance)
    if not success:
        raise HTTPException(500, instance.error_message or "Failed to start test")

    # Record in database
    if db:
        session = db.get_session()
        try:
            run = TestRun(
                name=instance_id,
                scenario_name=req.scenario,
                status="running",
                call_rate=req.call_rate,
                max_calls=req.max_calls,
                call_limit=req.call_limit,
                duration=req.duration,
                started_at=datetime.datetime.utcnow(),
            )
            session.add(run)
            session.commit()
        finally:
            session.close()

    return {"status": "started", "id": instance_id, "instance": instance.to_dict()}


@app.post("/api/tests/{test_id}/stop", dependencies=[Depends(require_api_key)])
def stop_test(test_id: str):
    """Stop a running test."""
    success = engine.stop_instance(test_id)
    if not success:
        raise HTTPException(404, f"Test '{test_id}' not found or not running")

    # Update DB
    if db:
        session = db.get_session()
        try:
            run = session.query(TestRun).filter_by(name=test_id).first()
            if run:
                inst = engine.get_instance(test_id)
                run.status = "stopped"
                run.completed_at = datetime.datetime.utcnow()
                if inst:
                    run.total_calls = inst.stats.total_calls
                    run.successful_calls = inst.stats.successful_calls
                    run.failed_calls = inst.stats.failed_calls
                session.commit()
        finally:
            session.close()

    return {"status": "stopped", "id": test_id}


@app.post("/api/tests/{test_id}/rate", dependencies=[Depends(require_api_key)])
def update_rate(test_id: str, req: UpdateRateRequest):
    """Update the call rate of a running test."""
    success = engine.update_call_rate(test_id, req.call_rate)
    if not success:
        raise HTTPException(404, f"Test '{test_id}' not found or not running")
    return {"status": "updated", "id": test_id, "call_rate": req.call_rate}


def _is_loop_managed(test_id: str) -> bool:
    """The loop answer side ('loop-uas') and per-campaign dialers ('uac-loop-*')
    are owned by the LoopEngine and surfaced on the Loops page. They must NOT
    appear in — or be deletable from — the one-shot test list: deleting the UAS
    is futile (the engine restarts it to keep answering), which is why it kept
    'showing up' again after a delete.
    """
    return test_id == "loop-uas" or test_id.startswith("uac-loop-")


@app.get("/api/tests", dependencies=[Depends(require_api_key)])
def list_tests():
    """List one-shot test instances (loop UAS/UAC are excluded — see Loops page)."""
    return {
        "tests": [
            t for t in engine.list_instances()
            if not _is_loop_managed(t.get("id", ""))
        ]
    }


@app.get("/api/tests/{test_id}", dependencies=[Depends(require_api_key)])
def get_test(test_id: str):
    """Get details of a specific test."""
    instance = engine.get_instance(test_id)
    if not instance:
        raise HTTPException(404, f"Test '{test_id}' not found")
    return instance.to_dict()


@app.delete("/api/tests/{test_id}", dependencies=[Depends(require_api_key)])
def remove_test(test_id: str):
    """Remove a stopped test instance (loop UAS/UAC are engine-managed — refused)."""
    if _is_loop_managed(test_id):
        raise HTTPException(
            409,
            f"'{test_id}' is managed by the loop engine and cannot be deleted here "
            "(the answer side is restarted automatically). Manage loop traffic on "
            "the Loops page.",
        )
    success = engine.remove_instance(test_id)
    if not success:
        raise HTTPException(400, f"Cannot remove '{test_id}' (still running or not found)")
    return {"status": "removed", "id": test_id}


@app.post("/api/tests/stop-all", dependencies=[Depends(require_api_key)])
def stop_all_tests():
    """Emergency stop all running tests."""
    engine.stop_all()
    return {"status": "all_stopped"}


# ─── Stats ─────────────────────────────────────────────────────────────────────

@app.get("/api/stats", dependencies=[Depends(require_api_key)])
def get_stats():
    """Get current aggregated stats."""
    return stats.get_current()


@app.get("/api/stats/history", dependencies=[Depends(require_api_key)])
def get_stats_history(limit: int = Query(default=100, ge=1, le=10000)):
    """Get stats history for charting."""
    return {"history": stats.get_history(limit)}


# ─── Scenarios ─────────────────────────────────────────────────────────────────

@app.get("/api/scenarios", dependencies=[Depends(require_api_key)])
def list_scenarios_api():
    """List all available SIP scenarios."""
    return {"scenarios": scenarios.list_scenarios()}


@app.get("/api/scenarios/{name}", dependencies=[Depends(require_api_key)])
def get_scenario(name: str):
    """Get the XML content of a scenario."""
    content = scenarios.get_scenario_content(name)
    if content is None:
        raise HTTPException(404, f"Scenario '{name}' not found")
    return {"name": name, "content": content}


@app.post("/api/scenarios", dependencies=[Depends(require_api_key)])
def create_scenario(req: ScenarioRequest):
    """Save a custom scenario."""
    path = scenarios.save_custom_scenario(req.name, req.xml_content)

    # Also save to DB
    if db:
        session = db.get_session()
        try:
            existing = session.query(Scenario).filter_by(name=req.name).first()
            if existing:
                existing.xml_content = req.xml_content
                existing.description = req.description
                existing.mode = req.mode
            else:
                sc = Scenario(
                    name=req.name,
                    description=req.description,
                    xml_content=req.xml_content,
                    mode=req.mode,
                )
                session.add(sc)
            session.commit()
        finally:
            session.close()

    return {"status": "saved", "name": req.name, "path": path}


@app.delete("/api/scenarios/{name}", dependencies=[Depends(require_api_key)])
def delete_scenario(name: str):
    """Delete a custom scenario."""
    success = scenarios.delete_custom_scenario(name)
    if not success:
        raise HTTPException(404, f"Scenario '{name}' not found or is built-in")
    return {"status": "deleted", "name": name}


# ─── Connectors ────────────────────────────────────────────────────────────────

@app.get("/api/connectors", dependencies=[Depends(require_api_key)])
def list_connectors():
    """List all configured connectors."""
    if not db:
        return {"connectors": []}
    session = db.get_session()
    try:
        connectors = session.query(Connector).all()
        return {"connectors": [c.to_dict() for c in connectors]}
    finally:
        session.close()


@app.post("/api/connectors", dependencies=[Depends(require_api_key)])
def create_connector(req: ConnectorRequest):
    """Create a new connector."""
    if not db:
        raise HTTPException(500, "Database not configured")
    session = db.get_session()
    try:
        c = Connector(
            name=req.name,
            description=req.description,
            local_ip=req.local_ip,
            local_port=req.local_port,
            remote_ip=req.remote_ip,
            remote_port=req.remote_port,
            transport=req.transport,
            auth_user=req.auth_user,
            auth_pass=req.auth_pass,
        )
        session.add(c)
        session.commit()
        return {"status": "created", "connector": c.to_dict()}
    except Exception as e:
        session.rollback()
        raise HTTPException(400, str(e))
    finally:
        session.close()


@app.delete("/api/connectors/{name}", dependencies=[Depends(require_api_key)])
def delete_connector(name: str):
    """Delete a connector."""
    if not db:
        raise HTTPException(500, "Database not configured")
    session = db.get_session()
    try:
        c = session.query(Connector).filter_by(name=name).first()
        if not c:
            raise HTTPException(404, f"Connector '{name}' not found")
        session.delete(c)
        session.commit()
        return {"status": "deleted", "name": name}
    finally:
        session.close()


# ─── Servers (source-IP "nodes") ────────────────────────────────────────────────

def _detect_source_ips() -> list[str]:
    """Best-effort list of the box's bound IPv4 addresses (for the Add-Server
    suggestion list). Loopback and link-local are filtered out."""
    ips: set[str] = set()
    try:
        import socket
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:
        import psutil  # optional; richer per-NIC enumeration when present
        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                if getattr(a, "family", None) == 2:  # AF_INET
                    ips.add(a.address)
    except Exception:
        pass
    return sorted(
        ip for ip in ips
        if not ip.startswith("127.") and not ip.startswith("169.254.")
    )


@app.get("/api/source-ips", dependencies=[Depends(require_api_key)])
def list_source_ips():
    """Auto-detected source IPv4s on this box (suggestions for adding servers)."""
    return {"source_ips": _detect_source_ips()}


@app.get("/api/servers", dependencies=[Depends(require_api_key)])
def list_servers():
    """List origination servers (a server = a source IP a loop can run from)."""
    if not db:
        return {"servers": []}
    session = db.get_session()
    try:
        return {"servers": [s.to_dict() for s in session.query(Server).all()]}
    finally:
        session.close()


def _ip_has_running_loop(ip: str) -> bool:
    """True if a loop campaign is currently RUNNING on source IP ``ip`` (DB-backed
    so it holds across worker restarts). Used to refuse delete/regen of a busy
    node. Best-effort: returns False if the DB is unavailable."""
    if not db or not ip:
        return False
    from sqlalchemy import text
    try:
        with db.engine.connect() as conn:
            row = conn.execute(
                text("SELECT 1 FROM loop_campaigns "
                     "WHERE local_ip = :ip AND status = 'running' LIMIT 1"),
                {"ip": ip},
            ).fetchone()
        return row is not None
    except Exception:
        return False


def _generate_node_pool(s: Server, origin_zone: str, dest_zone: str,
                        count: int, length: int,
                        origin_code: str = "", dest_code: str = "") -> None:
    """Generate ``s``'s number pool from its zones (optionally pinned to a single
    code per side) and store path/count on it. Removes the node's previous pool
    file. Raises HTTPException(422) on an unknown zone / impossible request."""
    from gencall.scripts.gen_loop_csv import generate_pool_file

    try:
        path, n, _preview = generate_pool_file(
            origin_zone=origin_zone, dest_zone=dest_zone,
            origin_code=origin_code or "", dest_code=dest_code or "",
            count=count, length=length,
        )
    except (ValueError, RuntimeError) as e:
        raise HTTPException(422, str(e))
    except FileNotFoundError as e:
        raise HTTPException(503, str(e))
    _unlink_quiet(s.csv_path)  # drop the superseded pool file
    s.origin_zone = origin_zone
    s.dest_zone = dest_zone
    s.origin_code = origin_code or ""
    s.dest_code = dest_code or ""
    s.pool_count = n
    s.pool_length = length
    s.csv_path = path


def _unlink_quiet(path: str) -> None:
    """Remove a generated pool file if it exists, ignoring errors."""
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


@app.post("/api/servers", dependencies=[Depends(require_api_key)])
def create_server(req: ServerRequest):
    """Register a node (a source IP). If origin_zone + dest_zone are supplied,
    its A/B number pool is generated immediately (each node = one loop's numbers)."""
    if not db:
        raise HTTPException(500, "Database not configured")
    name = (req.name or "").strip()
    ip = (req.ip or "").strip()
    if not name or not ip:
        raise HTTPException(422, "name and ip are required")
    session = db.get_session()
    try:
        s = Server(name=name, ip=ip, description=req.description or "",
                   group_id=req.group_id, pool_length=req.length)
        if req.origin_zone and req.dest_zone:
            _generate_node_pool(s, req.origin_zone, req.dest_zone,
                                req.count, req.length,
                                origin_code=req.origin_code, dest_code=req.dest_code)
        session.add(s)
        session.commit()
        return {"status": "created", "server": s.to_dict()}
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(400, f"could not create server (duplicate name?): {e}")
    finally:
        session.close()


@app.post("/api/servers/{server_id}/generate", dependencies=[Depends(require_api_key)])
def generate_server_pool(server_id: int, req: GeneratePoolRequest):
    """(Re)generate a node's number pool. Empty zones/length reuse the node's
    stored values, so a bare POST refreshes the existing pool."""
    if not db:
        raise HTTPException(500, "Database not configured")
    session = db.get_session()
    try:
        s = session.query(Server).filter_by(id=server_id).first()
        if not s:
            raise HTTPException(404, f"Server {server_id} not found")
        if _ip_has_running_loop(s.ip):
            raise HTTPException(
                409, f"Node '{s.name}' ({s.ip}) has a running loop — stop it before "
                "regenerating its numbers (the live loop keeps its current pool)."
            )
        origin = (req.origin_zone or s.origin_zone or "").strip()
        dest = (req.dest_zone or s.dest_zone or "").strip()
        if not origin or not dest:
            raise HTTPException(422, "origin_zone and dest_zone are required")
        length = req.length or s.pool_length or 11
        # Re-pin to the requested code, else keep the node's stored pin.
        ocode = req.origin_code if req.origin_code != "" else (s.origin_code or "")
        dcode = req.dest_code if req.dest_code != "" else (s.dest_code or "")
        _generate_node_pool(s, origin, dest, req.count, length,
                            origin_code=ocode, dest_code=dcode)
        session.commit()
        return {"status": "generated", "server": s.to_dict()}
    except HTTPException:
        session.rollback()
        raise
    finally:
        session.close()


class ServerUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    group_id: Optional[int] = None  # -1 clears membership; None leaves unchanged
    enabled: Optional[bool] = None


@app.put("/api/servers/{server_id}", dependencies=[Depends(require_api_key)])
def update_server(server_id: int, req: ServerUpdate):
    """Update a node's name/description/group/enabled (not its pool — use
    /generate for that). ``group_id`` of -1 clears group membership."""
    if not db:
        raise HTTPException(500, "Database not configured")
    session = db.get_session()
    try:
        s = session.query(Server).filter_by(id=server_id).first()
        if not s:
            raise HTTPException(404, f"Server {server_id} not found")
        if req.name is not None:
            s.name = req.name.strip() or s.name
        if req.description is not None:
            s.description = req.description
        if req.enabled is not None:
            s.enabled = req.enabled
        if req.group_id is not None:
            s.group_id = None if req.group_id < 0 else req.group_id
        session.commit()
        return {"status": "updated", "server": s.to_dict()}
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(400, str(e))
    finally:
        session.close()


@app.delete("/api/servers/{server_id}", dependencies=[Depends(require_api_key)])
def delete_server(server_id: int):
    """Delete a server by id."""
    if not db:
        raise HTTPException(500, "Database not configured")
    session = db.get_session()
    try:
        s = session.query(Server).filter_by(id=server_id).first()
        if not s:
            raise HTTPException(404, f"Server {server_id} not found")
        if _ip_has_running_loop(s.ip):
            raise HTTPException(
                409, f"Node '{s.name}' ({s.ip}) has a running loop — stop it first."
            )
        pool_file = s.csv_path
        session.delete(s)
        session.commit()
        _unlink_quiet(pool_file)  # remove the node's generated pool file
        return {"status": "deleted", "id": server_id}
    finally:
        session.close()


# ─── Test History ──────────────────────────────────────────────────────────────

@app.get("/api/history", dependencies=[Depends(require_api_key)])
def get_test_history(limit: int = Query(default=50, ge=1, le=500)):
    """Get test run history from database."""
    if not db:
        return {"history": []}
    session = db.get_session()
    try:
        runs = session.query(TestRun).order_by(TestRun.created_at.desc()).limit(limit).all()
        return {"history": [r.to_dict() for r in runs]}
    finally:
        session.close()


# ─── System ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health_check():
    """Health check endpoint."""
    return {
        "status": "ok",
        "version": "2.0.0",
        "name": "GenCall",
        "active_tests": len([i for i in engine.instances.values()
                             if i.state == SIPpState.RUNNING]) if engine else 0,
    }
