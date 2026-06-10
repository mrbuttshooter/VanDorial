"""
Loop Campaign API router (design §4.4).

Mounts on the worker FastAPI app alongside the existing ``/api/tests/*`` routes.
Endpoints:

  * ``POST /api/loops``                 start a Loop Campaign (spawns the UAC).
  * ``POST /api/loops/{id}/stop``       stop a campaign (kills its UAC).
  * ``GET  /api/loops``                 list campaigns.
  * ``GET  /api/loops/{id}``            live status incl. the UAC's SIPp stats.
  * ``GET  /api/loops/{id}/records.csv`` export this campaign's call_records.
  * ``GET  /api/answer/status``         UAS health + current answered calls.

The router calls into the shared ``LoopEngine`` (wired in main.py). Auth reuses
the same ``require_api_key`` dependency the rest of the worker API uses.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator

from gencall.api.routes import require_api_key
from gencall.api.loop_validation import (
    DestHostError,
    validate_caps,
    validate_dest_host,
    validate_transport,
)
from gencall.core.config import Config
from gencall.core.loop_engine import CapExceeded, LoopEngine
from gencall.core.loop_matcher import LoopMatcher

logger = logging.getLogger("gencall.api.loops")

router = APIRouter()

# Wired in main.py (create_app) once the LoopEngine is constructed. None means
# the loop subsystem is not configured — endpoints then return 503.
loop_engine: Optional[LoopEngine] = None

# The shared LoopMatcher (design §4.3), wired in main.py alongside the engine.
# When present, GET /api/loops/{id} folds the latest loop_stats snapshot into the
# campaign's live status. None => no matcher (e.g. no DB); the field is omitted.
loop_matcher: Optional[LoopMatcher] = None


def _engine() -> LoopEngine:
    if loop_engine is None:
        raise HTTPException(503, "Loop engine not configured on this worker")
    return loop_engine


# ─── Request model ───────────────────────────────────────────────────────────

class StartLoopRequest(BaseModel):
    """Loop-campaign start request with bounded, security-validated inputs.

    Structural bounds (negatives, zero, port range, transport set) are enforced
    here and surface as 422. Config-dependent checks — the dest_host
    private/loopback/SSRF block and the rate/channel upper caps — run in the
    endpoint (they need the runtime Config) and also surface as 422.
    """

    name: str = ""
    dest_host: str
    # Port must be a real, routable port (1-65535); 0 / negatives rejected.
    dest_port: int = Field(default=5060, ge=1, le=65535)
    transport: str = "udp"
    csv_path: str = ""
    # Rate must be positive (a 0/negative rate is a misconfiguration, never an
    # "until stopped" sentinel). Upper bound is enforced against config.
    rate: float = Field(default=1.0, gt=0)
    # At least one channel; upper bound enforced against the config channel cap.
    max_concurrent: int = Field(default=10, gt=0)
    duration_mode: str = "fixed"       # fixed | range
    # Hold durations are non-negative seconds.
    duration_s: int = Field(default=180, ge=0)
    duration_max_s: int = Field(default=0, ge=0)  # used only for mode == range
    match_key: str = "exact"
    # Targets are non-negative; 0 == "until stopped".
    target_calls: int = Field(default=0, ge=0)
    target_minutes: int = Field(default=0, ge=0)

    @field_validator("transport")
    @classmethod
    def _check_transport(cls, v: str) -> str:
        # Reject an unknown transport with 422 rather than silently downgrading
        # to UDP (which could send a TLS-intended campaign in cleartext).
        return validate_transport(v)


# ─── Endpoints ───────────────────────────────────────────────────────────────

@router.post("/api/loops", dependencies=[Depends(require_api_key)])
def start_loop(req: StartLoopRequest):
    """Start a Loop Campaign. Refuses (409) when the concurrent cap is reached.

    Before spawning, validate the destination against the SSRF/private-range
    block and bound rate/channels against the config caps (both 422 on reject).
    """
    config = _engine().config or Config()
    # SSRF / open-originator guard: reject private/loopback/multicast/0.0.0.0
    # unless explicitly allow-listed.
    try:
        validate_dest_host(req.dest_host, config.loops_dest_allowlist)
    except DestHostError as e:
        raise HTTPException(422, str(e))
    # OOM guard: rate/channel upper bounds.
    try:
        validate_caps(req.rate, req.max_concurrent, config)
    except ValueError as e:
        raise HTTPException(422, str(e))

    try:
        campaign = _engine().start_campaign(
            name=req.name,
            dest_host=req.dest_host,
            dest_port=req.dest_port,
            transport=req.transport,
            csv_path=req.csv_path,
            rate=req.rate,
            max_concurrent=req.max_concurrent,
            duration_mode=req.duration_mode,
            duration_s=req.duration_s,
            duration_max_s=req.duration_max_s,
            match_key=req.match_key,
            target_calls=req.target_calls,
            target_minutes=req.target_minutes,
        )
    except CapExceeded as e:
        raise HTTPException(409, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return {"status": "started", "campaign": campaign}


@router.post("/api/loops/{campaign_id}/stop", dependencies=[Depends(require_api_key)])
def stop_loop(campaign_id: str):
    """Stop a running campaign."""
    try:
        campaign = _engine().stop_campaign(campaign_id)
    except KeyError:
        raise HTTPException(404, f"Loop campaign '{campaign_id}' not found")
    return {"status": "stopped", "campaign": campaign}


@router.get("/api/loops", dependencies=[Depends(require_api_key)])
def list_loops():
    """List all Loop Campaigns."""
    return {"campaigns": _engine().list_campaigns()}


@router.get("/api/loops/{campaign_id}", dependencies=[Depends(require_api_key)])
def get_loop(campaign_id: str):
    """Live status for one campaign incl. its UAC's SIPp stats + latest loop_stats.

    The ``loop_stats`` field carries the most recent matcher snapshot (out/in
    minutes, completion %, per-call delta percentiles, failures by SIP code —
    design §4.3); it is ``None`` until the matcher has run a pass (or when no
    matcher/DB is wired).
    """
    try:
        campaign = _engine().get_campaign(campaign_id)
    except KeyError:
        raise HTTPException(404, f"Loop campaign '{campaign_id}' not found")
    campaign["loop_stats"] = (
        loop_matcher.latest_stats(campaign_id) if loop_matcher is not None else None
    )
    return campaign


@router.get(
    "/api/loops/{campaign_id}/records.csv",
    dependencies=[Depends(require_api_key)],
    response_class=PlainTextResponse,
)
def export_loop_records(campaign_id: str):
    """Export this campaign's ``call_records`` as CSV (header + rows)."""
    csv_text = _engine().records_csv(campaign_id)
    return PlainTextResponse(
        content=csv_text,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{campaign_id}_records.csv"'
        },
    )


@router.get("/api/answer/status", dependencies=[Depends(require_api_key)])
def answer_status():
    """UAS health + current answered-call count (design §4.4)."""
    return _engine().answer_status()
