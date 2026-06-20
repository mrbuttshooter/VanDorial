"""
Loop Campaign API router (design §4.4).

Mounts on the worker FastAPI app alongside the existing ``/api/tests/*`` routes.
Endpoints:

  * ``POST /api/loops``                 start a Loop Campaign (spawns the UAC).
  * ``POST /api/loops/{id}/stop``       stop a campaign (kills its UAC).
  * ``GET  /api/loops``                 list campaigns.
  * ``GET  /api/loops/{id}``            live status incl. the UAC's SIPp stats.
  * ``GET  /api/answer/status``         UAS health + current answered calls.

The router calls into the shared ``LoopEngine`` (wired in main.py). Auth reuses
the same ``require_api_key`` dependency the rest of the worker API uses.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from gencall.api.routes import require_api_key
from gencall.api.loop_validation import (
    DestHostError,
    validate_caps,
    validate_dest_host,
    validate_transport,
)
from gencall.core.config import Config
from gencall.core.call_records import CallRecordParser
from gencall.core.loop_engine import CapExceeded, IPBusy, LoopEngine
from gencall.core.loop_matcher import LoopMatcher
from gencall.core import sale_zones as sale_zones_db
from gencall.scripts import gen_loop_csv

logger = logging.getLogger("gencall.api.loops")

router = APIRouter()

# Wired in main.py (create_app) once the LoopEngine is constructed. None means
# the loop subsystem is not configured — endpoints then return 503.
loop_engine: Optional[LoopEngine] = None

# The shared LoopMatcher (design §4.3), wired in main.py alongside the engine.
# When present, GET /api/loops/{id} folds the latest loop_stats snapshot into the
# campaign's live status. None => no matcher (e.g. no DB); the field is omitted.
loop_matcher: Optional[LoopMatcher] = None

# The shared CallRecordParser (design §4.2), wired in main.py. GET/POST
# /api/config/trust read/hot-apply its inbound trust whitelist + drop flag (the
# controller fans the whitelist out to every worker via this endpoint). None =>
# no parser configured on this worker; the endpoints return 503.
call_parser: Optional[CallRecordParser] = None

# The on-demand pcap CaptureManager (design Part 3), wired in main.py. None =>
# capture is not configured on this worker (e.g. a Windows dev box, or tcpdump
# absent); the capture endpoints then return 503 rather than crashing.
capture_manager = None  # gencall.core.capture.CaptureManager


def _engine() -> LoopEngine:
    if loop_engine is None:
        raise HTTPException(503, "Loop engine not configured on this worker")
    return loop_engine


def _lookup_node(node_id: int) -> Optional[dict]:
    """Resolve a node (Server row) to a dict, or None. Uses the engine's DB."""
    db = getattr(_engine(), "db", None)
    if db is None:
        raise HTTPException(503, "Database not configured on this worker")
    from gencall.db.models import Server

    session = db.get_session()
    try:
        s = session.query(Server).filter_by(id=node_id).first()
        if not s:
            return None
        d = s.to_dict()
        d["api_key"] = s.api_key  # internal: needed to proxy to a remote worker
        return d
    finally:
        session.close()


def _worker_post(api_url: str, api_key: str, path: str, payload: dict, timeout: float = 25.0):
    """Sync POST to a remote worker's API with its key (proxy a loop start)."""
    import httpx

    headers = {"X-API-Key": api_key} if api_key else {}
    with httpx.Client(verify=False, timeout=timeout) as c:
        resp = c.post(api_url.rstrip("/") + path, json=payload, headers=headers)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


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
    # Node ("each IP one loop"): when set, the loop's source IP AND number pool
    # are taken from this node (gencall.db.models.Server), overriding local_ip /
    # csv_path below. This is the primary path from the UI.
    node_id: Optional[int] = None
    # Source IP this loop originates from ("Node = IP"). "" => OS-routed (legacy
    # single-IP behaviour). The engine enforces one running loop per non-empty IP.
    local_ip: str = ""
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
    # Stream real RTP media (PCMA) on each call when True; signaling-only (no
    # media on the wire) when False — the default keeps existing loops cheap.
    rtp: bool = False
    # When rtp: loop the media across the whole call (True) vs play once (False).
    rtp_loop: bool = False

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

    # Resolve the node ("each IP one loop"): its source IP and number pool drive
    # the campaign. This is how the UI launches — pick a node, it carries both.
    local_ip = req.local_ip
    csv_path = req.csv_path
    if req.node_id is not None:
        node = _lookup_node(req.node_id)
        if node is None:
            raise HTTPException(404, f"Node {req.node_id} not found")
        local_ip = node["ip"]
        csv_path = node.get("csv_path") or ""
        if not csv_path:
            raise HTTPException(
                422, f"Node '{node['name']}' has no number pool — generate one first"
            )
        # REMOTE node (one controller, many workers): run the loop ON its worker.
        if node.get("api_url"):
            payload = {
                "name": req.name, "dest_host": req.dest_host, "dest_port": req.dest_port,
                "transport": req.transport, "local_ip": local_ip, "csv_path": csv_path,
                "rate": req.rate, "max_concurrent": req.max_concurrent,
                "duration_mode": req.duration_mode, "duration_s": req.duration_s,
                "duration_max_s": req.duration_max_s, "match_key": req.match_key,
                "target_calls": req.target_calls, "target_minutes": req.target_minutes,
                "rtp": req.rtp, "rtp_loop": req.rtp_loop,
            }
            try:
                res = _worker_post(node["api_url"], node.get("api_key", ""),
                                   "/api/loops", payload)
            except Exception as e:
                raise HTTPException(502, f"worker {node['api_url']} loop start failed: {e}")
            campaign = res.get("campaign") or res
            if isinstance(campaign, dict):
                campaign["remote"] = node["api_url"]
            return {"status": "started", "campaign": campaign}

    try:
        campaign = _engine().start_campaign(
            name=req.name,
            dest_host=req.dest_host,
            dest_port=req.dest_port,
            transport=req.transport,
            csv_path=csv_path,
            rate=req.rate,
            max_concurrent=req.max_concurrent,
            duration_mode=req.duration_mode,
            duration_s=req.duration_s,
            duration_max_s=req.duration_max_s,
            match_key=req.match_key,
            target_calls=req.target_calls,
            target_minutes=req.target_minutes,
            local_ip=local_ip,
            node_id=req.node_id,
            rtp=req.rtp,
            rtp_loop=req.rtp_loop,
        )
    except IPBusy as e:
        raise HTTPException(409, str(e))
    except CapExceeded as e:
        raise HTTPException(409, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return {"status": "started", "campaign": campaign}


# ─── Fleet capture (controller routes by box; download is STREAMED) ──────────
# The console talks to the controller; these resolve ``box`` ("local" => this
# box's manager, else a worker api_url => proxy with that worker's api_key) and
# mirror the fleet_stop_loop box-resolution pattern. The download never buffers
# the .pcap in the controller — it streams chunk-by-chunk straight through. They
# delegate to the worker handlers (capture_start/stop/list/delete/download)
# defined further below; module-level names resolve at call time. Declared
# BEFORE the /api/loops/{campaign_id} routes (esp. {campaign_id}/stop) so a
# 'fleet-capture/...' path is not swallowed as a campaign id — the same reason
# fleet-stop / history sit ahead of the {campaign_id} catch-alls.


class FleetCaptureReq(BaseModel):
    campaign_id: str
    box: str = "local"   # "local" = this box, else the worker's api_url
    capture_id: str = ""


def _worker_key(box: str) -> str:
    """The api_key for the worker whose api_url == box (for proxying), or ""."""
    from gencall.db.models import Server
    session = _db().get_session()
    try:
        s = session.query(Server).filter_by(api_url=box).first()
        return s.api_key if s else ""
    finally:
        session.close()


@router.post("/api/loops/fleet-capture/start", dependencies=[Depends(require_api_key)])
def fleet_capture_start(req: FleetCaptureReq):
    if not req.box or req.box == "local":
        return capture_start(req.campaign_id)              # reuse the worker handler
    return _worker_post(req.box, _worker_key(req.box),
                        f"/api/loops/{req.campaign_id}/capture/start", {})


@router.post("/api/loops/fleet-capture/stop", dependencies=[Depends(require_api_key)])
def fleet_capture_stop(req: FleetCaptureReq):
    if not req.box or req.box == "local":
        return capture_stop(req.campaign_id, req.capture_id)
    return _worker_post(req.box, _worker_key(req.box),
                        f"/api/loops/{req.campaign_id}/capture/{req.capture_id}/stop", {})


@router.get("/api/loops/fleet-capture/list", dependencies=[Depends(require_api_key)])
def fleet_capture_list(campaign_id: str, box: str = "local"):
    if not box or box == "local":
        return capture_list(campaign_id)
    import httpx
    with httpx.Client(verify=False, timeout=15.0) as c:
        r = c.get(box.rstrip("/") + f"/api/loops/{campaign_id}/captures",
                  headers={"X-API-Key": _worker_key(box)})
    r.raise_for_status()
    return r.json()


@router.delete("/api/loops/fleet-capture/delete", dependencies=[Depends(require_api_key)])
def fleet_capture_delete(req: FleetCaptureReq):
    if not req.box or req.box == "local":
        return capture_delete(req.campaign_id, req.capture_id)
    import httpx
    with httpx.Client(verify=False, timeout=15.0) as c:
        r = c.request("DELETE", req.box.rstrip("/") +
                      f"/api/loops/{req.campaign_id}/capture/{req.capture_id}",
                      headers={"X-API-Key": _worker_key(req.box)})
    r.raise_for_status()
    return r.json() if r.content else {"status": "deleted"}


@router.get("/api/loops/fleet-capture/download", dependencies=[Depends(require_api_key)])
def fleet_capture_download(campaign_id: str, capture_id: str, box: str = "local"):
    if not box or box == "local":
        return capture_download(campaign_id, capture_id)
    import httpx
    fname = f"{campaign_id}_{capture_id}.pcap"

    def _proxy():
        with httpx.Client(verify=False, timeout=None) as c:
            with c.stream("GET", box.rstrip("/") +
                          f"/api/loops/{campaign_id}/capture/{capture_id}/download",
                          headers={"X-API-Key": _worker_key(box)}) as r:
                r.raise_for_status()
                for chunk in r.iter_bytes(65536):
                    yield chunk

    return StreamingResponse(_proxy(), media_type="application/vnd.tcpdump.pcap",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


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


@router.get("/api/loops/history", dependencies=[Depends(require_api_key)])
def loop_history():
    """Past + present loop runs with their final loop_stats, newest first.

    Feeds the History tab's loop archive. Declared BEFORE /api/loops/{id} so the
    literal 'history' path is not swallowed as a campaign id.
    """
    runs = _engine().list_campaigns()
    if loop_matcher is not None:
        for c in runs:
            c["loop_stats"] = loop_matcher.latest_stats(c["id"])
    runs.sort(key=lambda c: (c.get("created_at") or ""), reverse=True)
    return {"runs": runs}


@router.get("/api/loops/fleet", dependencies=[Depends(require_api_key)])
def list_loops_fleet():
    """All loop runs across THIS box + every remote worker, each tagged with its
    ``box`` and carrying loop_stats. Remote workers are the distinct ``api_url``
    of the node (Server) rows; we pull each worker's /api/loops/history (which
    already folds loop_stats). Declared before /api/loops/{id}."""
    out = []
    for c in _engine().list_campaigns():
        c = dict(c)
        c["box"] = "local"
        c["loop_stats"] = (loop_matcher.latest_stats(c["id"])
                           if loop_matcher is not None else None)
        out.append(c)

    workers: dict = {}
    try:
        from gencall.db.models import Server
        session = _db().get_session()
        try:
            for s in session.query(Server).all():
                if s.api_url:
                    workers[s.api_url] = s.api_key
        finally:
            session.close()
    except HTTPException:
        pass

    import httpx
    for api_url, api_key in workers.items():
        try:
            with httpx.Client(verify=False, timeout=8.0) as cl:
                r = cl.get(api_url.rstrip("/") + "/api/loops/history",
                           headers={"X-API-Key": api_key} if api_key else {})
            if r.status_code == 200:
                for run in (r.json() or {}).get("runs", []):
                    run = dict(run)
                    run["box"] = api_url
                    out.append(run)
        except Exception:
            logger.debug("fleet loops fetch failed for %s", api_url, exc_info=True)

    out.sort(key=lambda c: (c.get("created_at") or ""), reverse=True)
    return {"campaigns": out}


@router.get("/api/fleet/resources", dependencies=[Depends(require_api_key)])
def fleet_resources():
    """Per-node CPU/RAM across the fleet for the Fleet page. Each node (Server
    row) reports its box: blank api_url = THIS box (local _box_resources()),
    otherwise the remote worker's /api/resources is polled. Resources are cached
    per box, so two IPs sharing one box don't poll it twice."""
    from gencall.api.routes import _box_resources
    import httpx

    nodes = []
    try:
        from gencall.db.models import Server
        session = _db().get_session()
        try:
            for s in session.query(Server).all():
                d = s.to_dict()
                d["api_key"] = s.api_key  # internal: proxy auth
                nodes.append(d)
        finally:
            session.close()
    except HTTPException:
        pass

    local: Optional[dict] = None        # this box, computed once on demand
    cache: dict = {}                    # api_url -> resources dict (with online/error)

    out = []
    for n in nodes:
        api_url = n.get("api_url") or ""
        if not api_url:
            if local is None:
                local = _box_resources()
                local["online"] = True
            res = dict(local)
        else:
            if api_url not in cache:
                try:
                    headers = {"X-API-Key": n["api_key"]} if n.get("api_key") else {}
                    with httpx.Client(verify=False, timeout=6.0) as cl:
                        r = cl.get(api_url.rstrip("/") + "/api/resources", headers=headers)
                    if r.status_code == 200:
                        d = dict(r.json() or {})
                        d["online"] = True
                        cache[api_url] = d
                    else:
                        cache[api_url] = {"online": False, "error": f"HTTP {r.status_code}"}
                except Exception as e:
                    cache[api_url] = {"online": False, "error": str(e)}
            res = dict(cache[api_url])
        out.append({
            "id": n.get("id"),
            "ip": n.get("ip"),
            "name": n.get("name"),
            "group_id": n.get("group_id"),
            "remote": bool(api_url),
            "box": api_url or "local",
            "online": res.pop("online", False),
            "error": res.pop("error", None),
            "hostname": res.get("hostname"),
            "cpu_percent": res.get("cpu_percent"),
            "cores": res.get("cores"),
            "load1": res.get("load1"),
            "mem_total_mb": res.get("mem_total_mb"),
            "mem_used_mb": res.get("mem_used_mb"),
            "mem_percent": res.get("mem_percent"),
        })
    return {"nodes": out}


class FleetStopRequest(BaseModel):
    campaign_id: str
    box: str = "local"   # "local" = this box, else the worker's api_url


@router.post("/api/loops/fleet-stop", dependencies=[Depends(require_api_key)])
def fleet_stop_loop(req: FleetStopRequest):
    """Stop a campaign on whichever box it runs on (local or a remote worker)."""
    if not req.box or req.box == "local":
        try:
            campaign = _engine().stop_campaign(req.campaign_id)
        except KeyError:
            raise HTTPException(404, f"Loop campaign '{req.campaign_id}' not found")
        return {"status": "stopped", "campaign": campaign}

    api_key = ""
    session = _db().get_session()
    try:
        from gencall.db.models import Server
        s = session.query(Server).filter_by(api_url=req.box).first()
        api_key = s.api_key if s else ""
    finally:
        session.close()
    try:
        return _worker_post(req.box, api_key,
                            f"/api/loops/{req.campaign_id}/stop", {})
    except Exception as e:
        raise HTTPException(502, f"worker {req.box} stop failed: {e}")


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


@router.get("/api/answer/status", dependencies=[Depends(require_api_key)])
def answer_status():
    """UAS health + current answered-call count (design §4.4)."""
    return _engine().answer_status()


# ─── On-demand pcap capture ("Trace", design Part 3) ─────────────────────────
# Per running loop, start/stop a tcpdump capture filtered to the loop's dest
# switch + its SIPp signalling/RTP ports, keep the .pcap on THIS worker, list it,
# stream it on request, and delete it. Captures are NEVER started automatically.
# The capture manager is wired in main.py; absent (Windows dev / no tcpdump) it
# is None and every endpoint returns a clean 503 instead of crashing.


def _capture_mgr():
    if capture_manager is None:
        raise HTTPException(503, "capture not configured on this worker")
    return capture_manager


@router.post("/api/loops/{campaign_id}/capture/start", dependencies=[Depends(require_api_key)])
def capture_start(campaign_id: str):
    """Start a tcpdump capture for a running loop (filtered to its dest switch)."""
    from gencall.core.capture import build_capture_filter
    try:
        c = _engine().get_campaign(campaign_id)
    except KeyError:
        raise HTTPException(404, f"loop campaign '{campaign_id}' not found")
    sipp = c.get("sipp") or {}
    bpf = build_capture_filter(
        dest_host=c.get("dest_host", ""), dest_port=c.get("dest_port", 5060),
        local_port=(sipp.get("local_port") or 0), media_port=(sipp.get("media_port") or 0),
        transport=c.get("transport", "udp"))
    try:
        cap = _capture_mgr().start(campaign_id, bpf)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    return {"status": "capturing", "capture": cap}


@router.post("/api/loops/{campaign_id}/capture/{capture_id}/stop", dependencies=[Depends(require_api_key)])
def capture_stop(campaign_id: str, capture_id: str):
    try:
        return {"status": "stopped", "capture": _capture_mgr().stop(capture_id)}
    except KeyError:
        raise HTTPException(404, f"capture '{capture_id}' not found")


@router.get("/api/loops/{campaign_id}/captures", dependencies=[Depends(require_api_key)])
def capture_list(campaign_id: str):
    return {"captures": _capture_mgr().list(campaign_id)}


@router.get("/api/loops/{campaign_id}/capture/{capture_id}/download", dependencies=[Depends(require_api_key)])
def capture_download(campaign_id: str, capture_id: str):
    import os
    try:
        path = _capture_mgr().path(capture_id)
    except KeyError:
        raise HTTPException(404, f"capture '{capture_id}' not found")
    if not os.path.isfile(path):
        raise HTTPException(404, "capture file not found")

    def _chunks():
        with open(path, "rb") as fh:
            while True:
                b = fh.read(65536)
                if not b:
                    break
                yield b

    return StreamingResponse(_chunks(), media_type="application/vnd.tcpdump.pcap",
                             headers={"Content-Disposition": f'attachment; filename="{capture_id}.pcap"'})


@router.delete("/api/loops/{campaign_id}/capture/{capture_id}", dependencies=[Depends(require_api_key)])
def capture_delete(campaign_id: str, capture_id: str):
    try:
        _capture_mgr().delete(capture_id)
    except KeyError:
        raise HTTPException(404, f"capture '{capture_id}' not found")
    return {"status": "deleted", "id": capture_id}


# ─── Node groups (group nodes by route; start/stop a whole group's loops) ─────

def _db():
    db = getattr(_engine(), "db", None)
    if db is None:
        raise HTTPException(503, "Database not configured on this worker")
    return db


class NodeGroupRequest(BaseModel):
    """A group of nodes sharing a destination route. Starting the group fans a
    loop out to every member node (each on its own IP + pool)."""
    name: str
    description: str = ""
    dest_host: str = ""
    dest_port: int = Field(default=5060, ge=1, le=65535)
    transport: str = "udp"
    rate: float = Field(default=1.0, gt=0)
    max_concurrent: int = Field(default=10, gt=0)
    duration_mode: str = "fixed"
    duration_s: int = Field(default=180, ge=0)
    duration_max_s: int = Field(default=0, ge=0)
    match_key: str = "exact"
    target_calls: int = Field(default=0, ge=0)
    target_minutes: int = Field(default=0, ge=0)


_GROUP_FIELDS = (
    "name", "description", "dest_host", "dest_port", "transport", "rate",
    "max_concurrent", "duration_mode", "duration_s", "duration_max_s",
    "match_key", "target_calls", "target_minutes",
)


def _group_view(group, members, running_ips):
    d = group.to_dict()
    d["nodes"] = [m.to_dict() for m in members]
    d["node_count"] = len(members)
    d["running_count"] = sum(1 for m in members if m.ip in running_ips)
    return d


def _running_ips() -> set:
    return {
        (c.get("local_ip") or "")
        for c in _engine().list_campaigns()
        if c.get("status") == "running" and c.get("local_ip")
    }


@router.get("/api/node-groups", dependencies=[Depends(require_api_key)])
def list_node_groups():
    """List node groups with their member nodes and running-loop counts."""
    from gencall.db.models import NodeGroup, Server

    session = _db().get_session()
    try:
        running = _running_ips()
        groups = session.query(NodeGroup).all()
        out = []
        for g in groups:
            members = session.query(Server).filter_by(group_id=g.id).all()
            out.append(_group_view(g, members, running))
        return {"groups": out}
    finally:
        session.close()


@router.post("/api/node-groups", dependencies=[Depends(require_api_key)])
def create_node_group(req: NodeGroupRequest):
    """Create a node group (a customer/route with shared loop settings)."""
    from gencall.db.models import NodeGroup

    name = (req.name or "").strip()
    if not name:
        raise HTTPException(422, "name is required")
    session = _db().get_session()
    try:
        g = NodeGroup(**{f: getattr(req, f) for f in _GROUP_FIELDS})
        g.name = name
        session.add(g)
        session.commit()
        return {"status": "created", "group": g.to_dict()}
    except Exception as e:
        session.rollback()
        raise HTTPException(400, f"could not create group (duplicate name?): {e}")
    finally:
        session.close()


@router.put("/api/node-groups/{group_id}", dependencies=[Depends(require_api_key)])
def update_node_group(group_id: int, req: NodeGroupRequest):
    """Replace a group's settings (name + shared route/loop fields)."""
    from gencall.db.models import NodeGroup

    session = _db().get_session()
    try:
        g = session.query(NodeGroup).filter_by(id=group_id).first()
        if not g:
            raise HTTPException(404, f"Group {group_id} not found")
        for f in _GROUP_FIELDS:
            setattr(g, f, getattr(req, f))
        session.commit()
        return {"status": "updated", "group": g.to_dict()}
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(400, str(e))
    finally:
        session.close()


@router.delete("/api/node-groups/{group_id}", dependencies=[Depends(require_api_key)])
def delete_node_group(group_id: int):
    """Delete a group. Member nodes are kept (their group_id is cleared)."""
    from gencall.db.models import NodeGroup, Server

    session = _db().get_session()
    try:
        g = session.query(NodeGroup).filter_by(id=group_id).first()
        if not g:
            raise HTTPException(404, f"Group {group_id} not found")
        for m in session.query(Server).filter_by(group_id=group_id).all():
            m.group_id = None
        session.delete(g)
        session.commit()
        return {"status": "deleted", "id": group_id}
    finally:
        session.close()


class GroupStartRequest(BaseModel):
    """Optionally start only a SUBSET of a group's nodes. ``node_ids`` = None or
    empty means "all member nodes" (the common case)."""
    node_ids: Optional[list[int]] = None


@router.post("/api/node-groups/{group_id}/start", dependencies=[Depends(require_api_key)])
def start_node_group(group_id: int, req: GroupStartRequest = Body(default=None)):
    """Start a loop on the group's member nodes (each on its own IP + pool), using
    the group's shared destination + loop settings. By default every member is
    started; pass ``node_ids`` to run only a subset. Returns a per-node result
    list; nodes without a pool or whose IP is already looping are reported, not
    fatal."""
    from gencall.db.models import NodeGroup, Server

    config = _engine().config or Config()
    wanted = set(req.node_ids) if (req and req.node_ids) else None
    session = _db().get_session()
    try:
        g = session.query(NodeGroup).filter_by(id=group_id).first()
        if not g:
            raise HTTPException(404, f"Group {group_id} not found")
        if not (g.dest_host or "").strip():
            raise HTTPException(422, "group has no destination host set")
        # Validate the shared destination + caps ONCE before fanning out.
        try:
            validate_dest_host(g.dest_host, config.loops_dest_allowlist)
            validate_caps(g.rate, g.max_concurrent, config)
        except (DestHostError, ValueError) as e:
            raise HTTPException(422, str(e))
        # Snapshot everything needed as plain values BEFORE closing the session
        # (no detached-ORM access while we spawn loops).
        group_name = g.name
        params = {f: getattr(g, f) for f in _GROUP_FIELDS if f not in ("name", "description")}
        members = [
            {"id": m.id, "name": m.name, "ip": m.ip,
             "csv_path": m.csv_path, "enabled": m.enabled,
             "api_url": m.api_url, "api_key": m.api_key}
            for m in session.query(Server).filter_by(group_id=group_id).all()
            if wanted is None or m.id in wanted
        ]
    finally:
        session.close()

    results = []
    for m in members:
        base = {"node": m["name"], "ip": m["ip"]}
        if not m["enabled"]:
            results.append({**base, "ok": False, "skipped": "disabled"})
            continue
        if not m["csv_path"]:
            results.append({**base, "ok": False, "skipped": "no number pool"})
            continue
        try:
            campaign = _engine().start_campaign(
                name=f"{group_name}-{m['name']}",
                local_ip=m["ip"], csv_path=m["csv_path"], node_id=m["id"], **params,
            )
            results.append({**base, "ok": True, "campaign_id": campaign["id"]})
        except IPBusy as e:
            results.append({**base, "ok": False, "skipped": str(e)})
        except (CapExceeded, RuntimeError) as e:
            results.append({**base, "ok": False, "error": str(e)})
    started = sum(1 for r in results if r.get("ok"))
    return {"status": "started", "group": group_name, "started": started,
            "total": len(members), "results": results}


@router.post("/api/node-groups/{group_id}/stop", dependencies=[Depends(require_api_key)])
def stop_node_group(group_id: int):
    """Stop every running loop on this group's member IPs."""
    from gencall.db.models import NodeGroup, Server

    session = _db().get_session()
    try:
        g = session.query(NodeGroup).filter_by(id=group_id).first()
        if not g:
            raise HTTPException(404, f"Group {group_id} not found")
        group_name = g.name
        member_ips = {m.ip for m in session.query(Server).filter_by(group_id=group_id).all()}
    finally:
        session.close()

    stopped = []
    for c in _engine().list_campaigns():
        if c.get("status") == "running" and (c.get("local_ip") or "") in member_ips:
            try:
                _engine().stop_campaign(c["id"])
                stopped.append(c["id"])
            except KeyError:
                pass
    return {"status": "stopped", "group": group_name, "stopped": len(stopped),
            "campaign_ids": stopped}


# ─── Loop presets (saved recipes; run on a node or a group) ──────────────────

class LoopPresetRequest(BaseModel):
    """A saved loop recipe: destination + ACD/rate/targets, but NO source. The
    node or group to run it on is chosen at run time (POST .../run)."""
    name: str
    description: str = ""
    dest_host: str = ""
    dest_port: int = Field(default=5060, ge=1, le=65535)
    transport: str = "udp"
    rate: float = Field(default=1.0, gt=0)
    max_concurrent: int = Field(default=10, gt=0)
    duration_mode: str = "fixed"
    duration_s: int = Field(default=180, ge=0)
    duration_max_s: int = Field(default=0, ge=0)
    match_key: str = "exact"
    target_calls: int = Field(default=0, ge=0)
    target_minutes: int = Field(default=0, ge=0)
    # Stream real RTP media (PCMA) on each call when True; signaling-only off.
    rtp: bool = False
    # When rtp: loop the media across the whole call (True) vs play once (False).
    rtp_loop: bool = False

    @field_validator("transport")
    @classmethod
    def _check_transport(cls, v: str) -> str:
        return validate_transport(v)


class RunPresetRequest(BaseModel):
    """Where to fire a preset: a single source-IP node, OR a group (optionally a
    subset of its members via ``node_ids``)."""
    node_id: Optional[int] = None
    group_id: Optional[int] = None
    node_ids: Optional[list[int]] = None


_PRESET_FIELDS = (
    "name", "description", "dest_host", "dest_port", "transport", "rate",
    "max_concurrent", "duration_mode", "duration_s", "duration_max_s",
    "match_key", "target_calls", "target_minutes", "rtp", "rtp_loop",
)

# The subset of preset fields passed straight to start_campaign (everything bar
# name/description, which are presentation only).
_PRESET_RUN_PARAMS = (
    "dest_host", "dest_port", "transport", "rate", "max_concurrent",
    "duration_mode", "duration_s", "duration_max_s", "match_key",
    "target_calls", "target_minutes", "rtp", "rtp_loop",
)


def _start_on_member(name: str, params: dict, member: dict) -> dict:
    """Start one loop on a member node; return a per-node result (never raises).

    ``member`` is a plain dict (id/name/ip/csv_path/enabled) snapshotted before
    the DB session closed. A disabled node, a node with no pool, or an IP already
    looping is reported (not fatal) so a group run carries on."""
    base = {"node": member["name"], "ip": member["ip"]}
    if not member.get("enabled", True):
        return {**base, "ok": False, "skipped": "disabled"}
    if not member.get("csv_path"):
        return {**base, "ok": False, "skipped": "no number pool"}
    # REMOTE member: start the loop on its worker.
    if member.get("api_url"):
        payload = {"name": name, "local_ip": member["ip"],
                   "csv_path": member["csv_path"], **params}
        try:
            res = _worker_post(member["api_url"], member.get("api_key", ""),
                               "/api/loops", payload)
            camp = res.get("campaign") if isinstance(res.get("campaign"), dict) else None
            return {**base, "ok": True, "remote": member["api_url"],
                    "campaign_id": camp.get("id") if camp else None}
        except Exception as e:
            return {**base, "ok": False, "error": str(e)}
    try:
        campaign = _engine().start_campaign(
            name=name, local_ip=member["ip"], csv_path=member["csv_path"],
            node_id=member["id"], **params,
        )
        return {**base, "ok": True, "campaign_id": campaign["id"]}
    except IPBusy as e:
        return {**base, "ok": False, "skipped": str(e)}
    except (CapExceeded, RuntimeError) as e:
        return {**base, "ok": False, "error": str(e)}


@router.get("/api/loop-presets", dependencies=[Depends(require_api_key)])
def list_loop_presets():
    """List saved loop presets (newest first)."""
    from gencall.db.models import LoopPreset

    session = _db().get_session()
    try:
        rows = session.query(LoopPreset).order_by(LoopPreset.id.desc()).all()
        return {"presets": [p.to_dict() for p in rows]}
    finally:
        session.close()


@router.post("/api/loop-presets", dependencies=[Depends(require_api_key)])
def create_loop_preset(req: LoopPresetRequest):
    """Create a saved loop preset."""
    from gencall.db.models import LoopPreset

    name = (req.name or "").strip()
    if not name:
        raise HTTPException(422, "name is required")
    session = _db().get_session()
    try:
        p = LoopPreset(**{f: getattr(req, f) for f in _PRESET_FIELDS})
        p.name = name
        session.add(p)
        session.commit()
        return {"status": "created", "preset": p.to_dict()}
    except Exception as e:
        session.rollback()
        raise HTTPException(400, f"could not create preset (duplicate name?): {e}")
    finally:
        session.close()


@router.put("/api/loop-presets/{preset_id}", dependencies=[Depends(require_api_key)])
def update_loop_preset(preset_id: int, req: LoopPresetRequest):
    """Replace a preset's fields."""
    from gencall.db.models import LoopPreset

    session = _db().get_session()
    try:
        p = session.query(LoopPreset).filter_by(id=preset_id).first()
        if not p:
            raise HTTPException(404, f"Preset {preset_id} not found")
        for f in _PRESET_FIELDS:
            setattr(p, f, getattr(req, f))
        session.commit()
        return {"status": "updated", "preset": p.to_dict()}
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(400, str(e))
    finally:
        session.close()


@router.delete("/api/loop-presets/{preset_id}", dependencies=[Depends(require_api_key)])
def delete_loop_preset(preset_id: int):
    """Delete a preset (does not touch any running campaign it launched)."""
    from gencall.db.models import LoopPreset

    session = _db().get_session()
    try:
        p = session.query(LoopPreset).filter_by(id=preset_id).first()
        if not p:
            raise HTTPException(404, f"Preset {preset_id} not found")
        session.delete(p)
        session.commit()
        return {"status": "deleted", "id": preset_id}
    finally:
        session.close()


@router.post("/api/loop-presets/{preset_id}/run", dependencies=[Depends(require_api_key)])
def run_loop_preset(preset_id: int, req: RunPresetRequest = Body(default=None)):
    """Run a saved preset on a chosen source-IP node OR a group.

    The preset supplies the recipe (dest/ACD/rate/targets); the node or group
    supplies the source IP + number pool. Running on a group fans the recipe out
    to every member node (one loop per IP). Returns a per-node result list."""
    from gencall.db.models import LoopPreset, NodeGroup, Server

    req = req or RunPresetRequest()
    config = _engine().config or Config()
    session = _db().get_session()
    try:
        p = session.query(LoopPreset).filter_by(id=preset_id).first()
        if not p:
            raise HTTPException(404, f"Preset {preset_id} not found")
        if not (p.dest_host or "").strip():
            raise HTTPException(422, "preset has no destination host set")
        preset_name = p.name
        params = {f: getattr(p, f) for f in _PRESET_RUN_PARAMS}
        # Validate the destination + caps ONCE before launching.
        try:
            validate_dest_host(params["dest_host"], config.loops_dest_allowlist)
            validate_caps(params["rate"], params["max_concurrent"], config)
        except (DestHostError, ValueError) as e:
            raise HTTPException(422, str(e))
        # Resolve the target(s): a single node, or a group's (optionally subset) members.
        if req.node_id is not None:
            n = session.query(Server).filter_by(id=req.node_id).first()
            if not n:
                raise HTTPException(404, f"Node {req.node_id} not found")
            members = [{"id": n.id, "name": n.name, "ip": n.ip,
                        "csv_path": n.csv_path, "enabled": n.enabled,
                        "api_url": n.api_url, "api_key": n.api_key}]
        elif req.group_id is not None:
            g = session.query(NodeGroup).filter_by(id=req.group_id).first()
            if not g:
                raise HTTPException(404, f"Group {req.group_id} not found")
            wanted = set(req.node_ids) if req.node_ids else None
            members = [
                {"id": m.id, "name": m.name, "ip": m.ip,
                 "csv_path": m.csv_path, "enabled": m.enabled,
                 "api_url": m.api_url, "api_key": m.api_key}
                for m in session.query(Server).filter_by(group_id=g.id).all()
                if wanted is None or m.id in wanted
            ]
            if not members:
                raise HTTPException(422, "group has no member nodes to run")
        else:
            raise HTTPException(422, "pick a node_id or group_id to run the preset on")
    finally:
        session.close()

    results = [_start_on_member(f"{preset_name}-{m['name']}", params, m) for m in members]
    started = sum(1 for r in results if r.get("ok"))
    return {"status": "started", "preset": preset_name, "started": started,
            "total": len(members), "results": results}


# ─── Sale zones + number generation (web "drop zone" flow) ───────────────────

# The deck is loaded once and cached (it is large and read-only).
_ZONES_CACHE: Optional[dict] = None


def _zones():
    global _ZONES_CACHE
    if _ZONES_CACHE is None:
        _ZONES_CACHE = gen_loop_csv.load_zones(gen_loop_csv.resolve_deck_path())
    return _ZONES_CACHE


class GenerateNumbersRequest(BaseModel):
    """Generate an A/B number pool from a chosen origin + drop sale zone."""
    origin_zone: str
    dest_zone: str
    origin_code: str = ""          # optional: pin one code instead of spreading
    dest_code: str = ""
    count: int = Field(default=500000, ge=1, le=5_000_000)
    length: int = Field(default=11, ge=4, le=18)
    seed: Optional[int] = None


def _merged_catalog():
    """(zones, country_overrides, country_tree) = CSV deck + DB overlay."""
    base = _zones()                                  # cached CSV {zone: [codes]}
    db = getattr(_engine(), "db", None)
    if db is None:
        merged = gen_loop_csv.merge_zones(base, {})
        return merged, {}, gen_loop_csv.build_country_tree(merged)
    db_zones, overrides = sale_zones_db.db_catalog(db)
    merged = gen_loop_csv.merge_zones(base, db_zones)
    tree = gen_loop_csv.build_country_tree(merged, country_overrides=overrides)
    return merged, overrides, tree


@router.get("/api/sale-zones", dependencies=[Depends(require_api_key)])
def sale_zones():
    """Country -> [sale zones] tree + zone -> [codes] map (CSV deck + DB overlay)."""
    try:
        merged, _overrides, tree = _merged_catalog()
    except FileNotFoundError as e:
        raise HTTPException(503, str(e))
    return {
        "countries": [{"name": c, "zones": zs} for c, zs in tree.items()],
        "codes": {z: list(codes) for z, codes in merged.items()},
    }


class SaleZoneCreate(BaseModel):
    country: str
    zone: str
    code: str

    @field_validator("country", "zone")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("must not be empty")
        return v

    @field_validator("code")
    @classmethod
    def _digits(cls, v: str) -> str:
        v = (v or "").strip()
        if not v.isdigit():
            raise ValueError("code must be digits")
        return v


@router.post("/api/sale-zones", dependencies=[Depends(require_api_key)])
def create_sale_zone(req: SaleZoneCreate):
    """Add a user sale zone (overlay on the CSV deck). 409 on duplicate (zone, code)."""
    from sqlalchemy.exc import IntegrityError
    from gencall.db.models import SaleZone

    db = getattr(_engine(), "db", None)
    if db is None:
        raise HTTPException(503, "Database not configured on this worker")
    session = db.get_session()
    try:
        row = SaleZone(country=req.country, zone=req.zone, code=req.code)
        session.add(row)
        session.commit()
        return {"status": "created", "sale_zone": row.to_dict()}
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, f"zone/code already exists: {req.zone} / {req.code}")
    finally:
        session.close()


@router.delete("/api/sale-zones/{sale_zone_id}", dependencies=[Depends(require_api_key)])
def delete_sale_zone(sale_zone_id: int):
    """Delete a user-added sale zone by id (bundled CSV zones are not deletable)."""
    from gencall.db.models import SaleZone

    db = getattr(_engine(), "db", None)
    if db is None:
        raise HTTPException(503, "Database not configured on this worker")
    session = db.get_session()
    try:
        row = session.query(SaleZone).filter_by(id=sale_zone_id).first()
        if not row:
            raise HTTPException(404, f"sale zone {sale_zone_id} not found")
        session.delete(row)
        session.commit()
        return {"status": "deleted", "id": sale_zone_id}
    finally:
        session.close()


@router.post("/api/loops/numbers", dependencies=[Depends(require_api_key)])
def generate_numbers(req: GenerateNumbersRequest):
    """Generate an A/B number pool server-side and return its path + a preview.

    The returned ``csv_path`` is fed straight into a loop campaign's ``csv_path``;
    each call then draws a random pair from the pool (the engine renders a RANDOM
    -inf). Numbers are validated to start with a real zone code so MADA routes
    them to the chosen drop zone.
    """
    try:
        path, n, preview = gen_loop_csv.generate_pool_file(
            origin_zone=req.origin_zone, dest_zone=req.dest_zone,
            origin_code=req.origin_code, dest_code=req.dest_code,
            count=req.count, length=req.length, seed=req.seed,
        )
    except (ValueError, RuntimeError) as e:
        raise HTTPException(422, str(e))
    except FileNotFoundError as e:
        raise HTTPException(503, str(e))

    return {
        "csv_path": path,
        "count": n,
        "origin_zone": req.origin_zone,
        "dest_zone": req.dest_zone,
        "preview": preview,
    }


# ─── Inbound trust whitelist (controller push, design §4.1 / §5.3) ───────────


class TrustConfigBody(BaseModel):
    ips: list[str] = []
    drop_untrusted: bool = False


@router.get("/api/config/trust", dependencies=[Depends(require_api_key)])
def get_trust_config():
    """This worker's current inbound trust whitelist + drop flag."""
    if call_parser is None:
        raise HTTPException(503, "call-record parser not configured on this worker")
    return call_parser.get_trust()


@router.post("/api/config/trust", dependencies=[Depends(require_api_key)])
def set_trust_config(body: TrustConfigBody):
    """Hot-apply an inbound trust whitelist (controller push). Empty ips = allow-all."""
    import ipaddress
    if call_parser is None:
        raise HTTPException(503, "call-record parser not configured on this worker")
    for tok in body.ips:
        try:
            ipaddress.ip_network((tok or "").strip(), strict=False)
        except ValueError:
            raise HTTPException(422, f"invalid IP/CIDR: {tok!r}")
    call_parser.set_trust([t.strip() for t in body.ips if t.strip()], body.drop_untrusted)
    return {"status": "applied", **call_parser.get_trust()}
