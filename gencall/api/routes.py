"""
GenCall REST API Routes.
FastAPI-based API for controlling the traffic generator.
"""

import uuid
import datetime
import logging
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Header, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from gencall.core.sipp_engine import (
    SIPpEngine, SIPpInstance, SIPpMode, SIPpTransport, SIPpState
)
from gencall.core.stats import StatsEngine
from gencall.core.api_gateway import APIGateway
from gencall.scenarios.manager import ScenarioManager
from gencall.db.models import Database, Connector, Scenario, TestRun, User

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
