"""Security regression: bounded/validated API inputs (pentest hardening round 3).

These lock the fixes that don't touch the engine or call flow:

  * worker/node URLs are SSRF-guarded on UPDATE (not just create), on both the
    worker (`update_server`) and controller (`create_node`/`update_node`).
  * node / loop source IPs must be real IP literals.
  * the ad-hoc test endpoint no longer forwards a free-form `extra_args` (SIPp
    argument injection) and rejects an unknown transport.
  * sizing + pool-count inputs are bounded.
  * a directly-supplied loop `csv_path` is contained to the pools directory.
"""

import ipaddress

import pytest
from pydantic import ValidationError


# ── IP-literal validation on node / loop source IPs ──────────────────────────

def test_server_request_rejects_non_ip():
    from gencall.api.routes import ServerRequest
    with pytest.raises(ValidationError):
        ServerRequest(name="n", ip="not-an-ip")
    # a real literal is accepted and a blank ip is refused (required)
    ServerRequest(name="n", ip="10.35.21.8")
    with pytest.raises(ValidationError):
        ServerRequest(name="n", ip="")


def test_start_loop_request_local_ip_validation():
    from gencall.api.loops import StartLoopRequest
    # blank == OS-routed, allowed; a real literal allowed; junk rejected
    StartLoopRequest(dest_host="208.87.169.100", local_ip="")
    StartLoopRequest(dest_host="208.87.169.100", local_ip="10.35.21.8")
    with pytest.raises(ValidationError):
        StartLoopRequest(dest_host="208.87.169.100", local_ip="banana")


# ── extra_args removed from the ad-hoc test request (argument injection) ──────

def test_start_test_request_has_no_extra_args():
    from gencall.api.routes import StartTestRequest
    m = StartTestRequest(remote_host="208.87.169.100", extra_args="-trace_msg /etc/x")
    # the field is dropped, so a smuggled value never reaches the model/engine
    assert not hasattr(m, "extra_args")


# ── bounded sizing / pool counts ─────────────────────────────────────────────

def test_traffic_calc_request_upper_bounds():
    from gencall.api.loops import TrafficCalcRequest
    TrafficCalcRequest(target_minutes=100, acd_s=60)
    with pytest.raises(ValidationError):
        TrafficCalcRequest(target_minutes=100, acd_s=90_000)   # ACD > 1 day
    with pytest.raises(ValidationError):
        TrafficCalcRequest(target_minutes=10**12, acd_s=60)


def test_generate_numbers_count_capped_at_2m():
    from gencall.api.loops import GenerateNumbersRequest
    GenerateNumbersRequest(origin_zone="a", dest_zone="b", count=2_000_000)
    with pytest.raises(ValidationError):
        GenerateNumbersRequest(origin_zone="a", dest_zone="b", count=2_000_001)


# ── controller node address SSRF guard (parity with worker create_server) ─────

def test_reject_unsafe_worker_url_used_by_controller():
    # The controller imports the same guard; confirm it blocks metadata/link-local.
    from gencall.controller.routes import _reject_unsafe_worker_url
    from fastapi import HTTPException
    for bad in ("http://169.254.169.254/", "http://0.0.0.0:8080"):
        with pytest.raises(HTTPException):
            _reject_unsafe_worker_url(bad)
    _reject_unsafe_worker_url("http://10.35.21.8:8080")   # real VLAN worker OK


# ── direct loop csv_path is contained to the pools dir ───────────────────────

def test_validate_pool_csv_path_rejects_traversal_and_missing(tmp_path, monkeypatch):
    from gencall.api import loops
    from fastapi import HTTPException

    base = tmp_path / "gencall_numbers"
    base.mkdir()
    monkeypatch.setattr(loops, "_pool_base_dir", lambda: str(base.resolve()))

    # outside the pools dir -> refused
    outside = tmp_path / "passwd"
    outside.write_text("x")
    with pytest.raises(HTTPException):
        loops._validate_pool_csv_path(str(outside))
    # inside but missing -> refused
    with pytest.raises(HTTPException):
        loops._validate_pool_csv_path(str(base / "nope.csv"))
    # inside and existing -> returned (real path)
    good = base / "numbers_ok.csv"
    good.write_text("a,b\n")
    assert loops._validate_pool_csv_path(str(good)) == str(good.resolve())
