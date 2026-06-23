"""Wave 1 security hardening (v2.2.5) — control-plane / validation / logging.

Every fix here is deliberately OFF the call path: none touch loop_uac.xml /
loop_uas.xml, the SIPp command we send, or campaign origination. The
engine/sipp/shaper/matcher suites remain the call-path regression guard.
"""
import pytest
from fastapi.testclient import TestClient

from gencall import __version__
from gencall.api import routes
from gencall.api.loop_validation import validate_caps
from gencall.core.config import Config


# ── #21: /api/health reports the REAL version (not a hardcoded literal) ───────

def test_health_reports_real_version():
    body = TestClient(routes.app).get("/api/health").json()
    assert body["version"] == __version__


# ── #20: API docs are not served (unauthenticated surface map) ────────────────

def test_api_docs_disabled():
    c = TestClient(routes.app)
    assert c.get("/docs").status_code == 404
    assert c.get("/openapi.json").status_code == 404


# ── #4: console bootstrap is local-only and fails CLOSED ──────────────────────

def test_bootstrap_is_local_only(monkeypatch):
    # No gateway (skip user-count); a configured legacy key. TestClient's peer is
    # "testclient" — i.e. NOT loopback — so a network caller must be refused.
    monkeypatch.setattr(routes, "gateway", None)
    monkeypatch.setattr(routes, "console_api_key", "legacy-key-xyz")
    r = TestClient(routes.app).get("/api/console/bootstrap")
    assert r.status_code == 404
    assert "legacy-key-xyz" not in r.text


def test_bootstrap_fails_closed_on_db_error(monkeypatch):
    class _Boom:
        def count_users(self):
            raise RuntimeError("db down")

    class _GW:
        users = _Boom()

    monkeypatch.setattr(routes, "gateway", _GW())
    monkeypatch.setattr(routes, "console_api_key", "legacy-key-xyz")
    r = TestClient(routes.app).get("/api/console/bootstrap")
    assert r.status_code == 503           # must NOT fall through to the key
    assert "legacy-key-xyz" not in r.text


# ── #14: loop caps reject runaway duration/targets, accept real campaigns ─────

def test_validate_caps_accepts_a_real_loop():
    # Your South Africa loop: ~90 s hold, 200k daily-minutes target — must pass.
    validate_caps(2.0, 200, Config(), duration_s=90, duration_max_s=0,
                  target_calls=0, target_minutes=200_000)


def test_validate_caps_rejects_runaway_duration():
    with pytest.raises(ValueError):
        validate_caps(2.0, 10, Config(), duration_s=2_000_000_000)


def test_validate_caps_rejects_absurd_target_minutes():
    with pytest.raises(ValueError):
        validate_caps(2.0, 10, Config(), target_minutes=10**12)


# ── #9: SIPp command log redacts -ap/-au; the REAL command is untouched ───────

def test_sipp_command_log_redacts_credentials():
    from gencall.core.sipp_engine import _redact_cmd
    cmd = ["sipp", "1.2.3.4:5060", "-au", "alice", "-ap", "s3cr3t", "-r", "1.0"]
    red = " ".join(_redact_cmd(cmd))
    assert "s3cr3t" not in red and "alice" not in red
    assert cmd[5] == "s3cr3t"             # original argv NOT mutated (engine sends real creds)


# ── Wave 2 #2: scenario-RCE filter decides on a real XML parse (regex bypass) ──

from gencall.scenarios.manager import reject_dangerous_scenario

_SHELL_ATTR = "command"   # the <exec> attribute SIPp would run as a shell verb


def _exec_scn(attrs):
    return f"<scenario><exec {attrs}/></scenario>"


def test_scenario_rejects_shell_exec_attr():
    with pytest.raises(ValueError):
        reject_dangerous_scenario(_exec_scn(f'{_SHELL_ATTR}="id"'))


def test_scenario_rejects_shell_attr_smuggled_past_gt():
    # Old regex bypass: a literal '>' inside an earlier attribute value let the
    # shell attr slip past the denylist. A real XML parse catches it.
    with pytest.raises(ValueError):
        reject_dangerous_scenario(_exec_scn(f'rtp_stream="x>y" {_SHELL_ATTR}="x"'))


def test_scenario_allows_benign_media_exec():
    reject_dangerous_scenario(_exec_scn('play_pcap_audio="a.pcap"'))   # no raise


def test_scenario_rejects_doctype_entity():
    with pytest.raises(ValueError):
        reject_dangerous_scenario('<!DOCTYPE x [<!ENTITY a "b">]><scenario/>')


# ── Wave 2 #10: discovery fails CLOSED with no fleet token (no "accept any") ───

from gencall.core.discovery import parse_beacon, encode_beacon, build_beacon

_ADDR = "http://10.0.0.9:8080"


def test_discovery_rejects_beacons_when_no_token_configured():
    beacon = encode_beacon(build_beacon("", _ADDR, "h", "v"))
    assert parse_beacon(beacon, "") is None          # empty token now REJECTS (was accept-any)


def test_discovery_accepts_matching_token():
    beacon = encode_beacon(build_beacon("fleet-tok", _ADDR, "h", "v"))
    out = parse_beacon(beacon, "fleet-tok")
    assert out is not None and out["address"] == _ADDR


def test_discovery_rejects_wrong_token():
    beacon = encode_beacon(build_beacon("wrong", _ADDR, "h", "v"))
    assert parse_beacon(beacon, "fleet-tok") is None
