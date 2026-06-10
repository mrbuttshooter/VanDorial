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
from pydantic import BaseModel

from gencall.api.routes import require_api_key
from gencall.core.loop_engine import CapExceeded, LoopEngine

logger = logging.getLogger("gencall.api.loops")

router = APIRouter()

# Wired in main.py (create_app) once the LoopEngine is constructed. None means
# the loop subsystem is not configured — endpoints then return 503.
loop_engine: Optional[LoopEngine] = None


def _engine() -> LoopEngine:
    if loop_engine is None:
        raise HTTPException(503, "Loop engine not configured on this worker")
    return loop_engine


# ─── Request model ───────────────────────────────────────────────────────────

class StartLoopRequest(BaseModel):
    name: str = ""
    dest_host: str
    dest_port: int = 5060
    transport: str = "udp"
    csv_path: str = ""
    rate: float = 1.0
    max_concurrent: int = 10
    duration_mode: str = "fixed"       # fixed | range
    duration_s: int = 180
    duration_max_s: int = 0            # used only for duration_mode == range
    match_key: str = "exact"
    target_calls: int = 0              # 0 = until stopped
    target_minutes: int = 0            # 0 = until stopped


# ─── Endpoints ───────────────────────────────────────────────────────────────

@router.post("/api/loops", dependencies=[Depends(require_api_key)])
def start_loop(req: StartLoopRequest):
    """Start a Loop Campaign. Refuses (409) when the concurrent cap is reached."""
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
    """Live status for one campaign incl. its UAC's current SIPp stats."""
    try:
        return _engine().get_campaign(campaign_id)
    except KeyError:
        raise HTTPException(404, f"Loop campaign '{campaign_id}' not found")


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
