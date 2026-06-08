"""
GenCall Test Suite

Tests core modules to verify they actually work:
  1. Config loading
  2. Database models + CRUD
  3. RTP packet building
  4. Scenario manager
  5. Stats engine
  6. SIP message parser
  7. REST API endpoints
  8. CDR engine
  9. SRTP encryption/decryption
  10. Codec negotiation
  11. Number pool
  12. Traffic profiles
"""

import os
import sys
import time
import json
import tempfile
import struct

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0
FAIL = 0
ERRORS = []


def test(name):
    """Decorator for test functions."""
    def wrapper(fn):
        global PASS, FAIL
        try:
            fn()
            PASS += 1
            print(f"  \033[92m[PASS]\033[0m {name}")
        except Exception as e:
            FAIL += 1
            ERRORS.append((name, str(e)))
            print(f"  \033[91m[FAIL]\033[0m {name}: {e}")
    return wrapper


# ═══════════════════════════════════════════════════════════════════════════════
#  1. CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

print("\n\033[1m=== Config ===\033[0m")

@test("Config loads defaults")
def _():
    from gencall.core.config import Config
    Config.reset()
    config = Config()
    assert config.web_port == 8080
    assert config.sip_t1 == 60
    assert config.db_engine == "sqlite"
    assert config.min_rtp_port == 10000
    Config.reset()

@test("Config reads from file")
def _():
    from gencall.core.config import Config
    Config.reset()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".cfg", delete=False) as f:
        f.write("[web]\nport = 9999\n[sip]\nT1 = 30\n")
        f.flush()
        config = Config(f.name)
        assert config.web_port == 9999
        assert config.sip_t1 == 30
    os.unlink(f.name)
    Config.reset()

@test("Config builds database URL")
def _():
    from gencall.core.config import Config
    Config.reset()
    config = Config()
    url = config.db_url
    assert "sqlite" in url
    Config.reset()


# ═══════════════════════════════════════════════════════════════════════════════
#  2. DATABASE
# ═══════════════════════════════════════════════════════════════════════════════

print("\n\033[1m=== Database ===\033[0m")

@test("Create tables and insert connector")
def _():
    from gencall.db.models import Database, Connector
    db = Database("sqlite:///")
    db.create_tables()
    session = db.get_session()
    c = Connector(name="test-conn", local_ip="10.0.0.1", remote_ip="10.0.0.2")
    session.add(c)
    session.commit()
    result = session.query(Connector).filter_by(name="test-conn").first()
    assert result is not None
    assert result.local_ip == "10.0.0.1"
    d = result.to_dict()
    assert d["name"] == "test-conn"
    session.close()

@test("Create and query test runs")
def _():
    from gencall.db.models import Database, TestRun
    db = Database("sqlite:///")
    db.create_tables()
    session = db.get_session()
    run = TestRun(name="test-run-1", scenario_name="basic_call", status="running",
                  call_rate=5.0, total_calls=100, successful_calls=95, failed_calls=5)
    session.add(run)
    session.commit()
    result = session.query(TestRun).first()
    assert result.name == "test-run-1"
    assert result.successful_calls == 95
    d = result.to_dict()
    assert d["call_rate"] == 5.0
    session.close()

@test("User model with password hash")
def _():
    from gencall.db.models import Database, User
    db = Database("sqlite:///")
    db.create_tables()
    session = db.get_session()
    u = User(username="admin", password_hash="abc123", role="admin")
    session.add(u)
    session.commit()
    result = session.query(User).filter_by(username="admin").first()
    assert result.role == "admin"
    session.close()


# ═══════════════════════════════════════════════════════════════════════════════
#  3. RTP
# ═══════════════════════════════════════════════════════════════════════════════

print("\n\033[1m=== RTP Engine ===\033[0m")

@test("RTP header construction")
def _():
    from gencall.core.rtp import rtp_header
    header = rtp_header(2, 0, 0, 0, 8, 1234, 160, 0xDEADBEEF)
    assert len(header) == 12
    # Verify version = 2 (top 2 bits of first byte)
    assert (header[0] >> 6) == 2
    # Verify payload type = 8
    assert (header[1] & 0x7F) == 8
    # Verify sequence number
    seq = struct.unpack(">H", header[2:4])[0]
    assert seq == 1234
    # Verify SSRC
    ssrc = struct.unpack(">L", header[8:12])[0]
    assert ssrc == 0xDEADBEEF

@test("Codec detection from filename")
def _():
    from gencall.core.rtp import detect_codec
    pt, bpm = detect_codec("test.g729")
    assert pt == 18
    assert bpm == 1
    pt, bpm = detect_codec("audio.g711u")
    assert pt == 0
    assert bpm == 8
    pt, bpm = detect_codec("audio.g711a")
    assert pt == 8
    assert bpm == 8

@test("DTMF streamer generates packets")
def _():
    from gencall.core.rtp import DTMFStreamer
    dtmf = DTMFStreamer("1", volume=10, duration_ms=160, payload_type=101)
    pkt = dtmf.get_replacement_packet(1000, 50, 0x12345678)
    assert pkt is not None
    assert len(pkt) > 12  # header + event data

@test("RTP port manager allocates and releases")
def _():
    from gencall.core.config import Config
    from gencall.core.rtp import RTPPortManager
    Config.reset()
    mgr = RTPPortManager()
    port1 = mgr.allocate()
    port2 = mgr.allocate()
    assert port1 != port2
    assert port1 % 2 == 0  # RTP ports are even
    assert port2 % 2 == 0
    avail_before = mgr.available
    mgr.release(port1)
    assert mgr.available == avail_before + 1
    Config.reset()

@test("IPv6 detection")
def _():
    from gencall.core.rtp import is_ipv6
    assert is_ipv6("::1") == True
    assert is_ipv6("2001:db8::1") == True
    assert is_ipv6("10.0.0.1") == False


# ═══════════════════════════════════════════════════════════════════════════════
#  4. SCENARIOS
# ═══════════════════════════════════════════════════════════════════════════════

print("\n\033[1m=== Scenarios ===\033[0m")

@test("Scenario manager finds built-in scenarios")
def _():
    from gencall.scenarios.manager import ScenarioManager
    mgr = ScenarioManager()
    scenarios = mgr.list_scenarios()
    names = [s["name"] for s in scenarios]
    assert "basic_call" in names
    assert "stress_test" in names
    assert "uas_answer" in names
    assert len(scenarios) >= 6

@test("Scenario manager reads XML content")
def _():
    from gencall.scenarios.manager import ScenarioManager
    mgr = ScenarioManager()
    content = mgr.get_scenario_content("basic_call")
    assert content is not None
    assert "INVITE" in content
    assert "GenCall" in content

@test("Scenario manager resolves paths")
def _():
    from gencall.scenarios.manager import ScenarioManager
    mgr = ScenarioManager()
    path = mgr.get_scenario_path("basic_call")
    assert path is not None
    assert os.path.exists(path)
    assert path.endswith(".xml")

@test("Custom scenario save and delete")
def _():
    from gencall.scenarios.manager import ScenarioManager
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = ScenarioManager(custom_dir=tmpdir)
        mgr.save_custom_scenario("my_test", "<scenario>test</scenario>")
        content = mgr.get_scenario_content("my_test")
        assert content == "<scenario>test</scenario>"
        mgr.delete_custom_scenario("my_test")
        assert mgr.get_scenario_content("my_test") is None


# ═══════════════════════════════════════════════════════════════════════════════
#  5. STATS ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

print("\n\033[1m=== Stats Engine ===\033[0m")

@test("Stats snapshot creation")
def _():
    from gencall.core.stats import StatsSnapshot
    snap = StatsSnapshot(
        timestamp=time.time(),
        active_instances=2,
        total_calls=1000,
        successful_calls=950,
        failed_calls=50,
        calls_per_second=10.5,
        success_rate=95.0,
    )
    d = snap.to_dict()
    assert d["active_instances"] == 2
    assert d["total_calls"] == 1000
    assert d["success_rate"] == 95.0

@test("Stats engine collects history")
def _():
    from gencall.core.config import Config
    from gencall.core.stats import StatsEngine
    Config.reset()
    engine = StatsEngine()
    assert engine.get_current()["total_calls"] == 0
    history = engine.get_history()
    assert isinstance(history, list)
    Config.reset()


# ═══════════════════════════════════════════════════════════════════════════════
#  6. SIP DEBUGGER
# ═══════════════════════════════════════════════════════════════════════════════

print("\n\033[1m=== SIP Debugger ===\033[0m")

@test("Parse SIP INVITE request")
def _():
    from gencall.core.sip_debug import SIPParser
    raw = (
        "INVITE sip:bob@10.0.0.2:5060 SIP/2.0\r\n"
        "Via: SIP/2.0/UDP 10.0.0.1:5060;branch=z9hG4bK776\r\n"
        "From: \"Alice\" <sip:alice@10.0.0.1>;tag=abc123\r\n"
        "To: <sip:bob@10.0.0.2>\r\n"
        "Call-ID: test-call-id-001@10.0.0.1\r\n"
        "CSeq: 1 INVITE\r\n"
        "Contact: <sip:alice@10.0.0.1:5060>\r\n"
        "Content-Type: application/sdp\r\n"
        "Content-Length: 0\r\n"
        "\r\n"
    )
    msg = SIPParser.parse(raw)
    assert msg.is_request
    assert msg.method == "INVITE"
    assert msg.call_id == "test-call-id-001@10.0.0.1"
    assert "alice" in msg.from_uri.lower()

@test("Parse SIP 200 OK response")
def _():
    from gencall.core.sip_debug import SIPParser
    raw = (
        "SIP/2.0 200 OK\r\n"
        "Via: SIP/2.0/UDP 10.0.0.1:5060;branch=z9hG4bK776\r\n"
        "From: \"Alice\" <sip:alice@10.0.0.1>;tag=abc123\r\n"
        "To: <sip:bob@10.0.0.2>;tag=def456\r\n"
        "Call-ID: test-call-id-002@10.0.0.1\r\n"
        "CSeq: 1 INVITE\r\n"
        "Content-Length: 0\r\n"
        "\r\n"
    )
    msg = SIPParser.parse(raw)
    assert not msg.is_request
    assert msg.status_code == 200
    assert msg.reason_phrase == "OK"
    assert msg.to_tag == "def456"

@test("Parse SIP with SDP body")
def _():
    from gencall.core.sip_debug import SIPParser
    raw = (
        "INVITE sip:bob@10.0.0.2 SIP/2.0\r\n"
        "Call-ID: sdp-test@10.0.0.1\r\n"
        "CSeq: 1 INVITE\r\n"
        "From: <sip:alice@10.0.0.1>;tag=abc\r\n"
        "To: <sip:bob@10.0.0.2>\r\n"
        "Content-Type: application/sdp\r\n"
        "Content-Length: 100\r\n"
        "\r\n"
        "v=0\r\n"
        "o=alice 123 456 IN IP4 10.0.0.1\r\n"
        "s=Test\r\n"
        "c=IN IP4 10.0.0.1\r\n"
        "m=audio 20000 RTP/AVP 8 0 101\r\n"
        "a=rtpmap:8 PCMA/8000\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
    )
    msg = SIPParser.parse(raw)
    assert msg.sdp is not None
    assert msg.sdp.media_port == 20000
    assert 8 in msg.sdp.codec_ids

@test("Hex dump generation")
def _():
    from gencall.core.sip_debug import _hex_dump
    result = _hex_dump(b"Hello, GenCall!", 16)
    assert "48 65 6c 6c" in result  # "Hell" in hex
    assert "|Hello, GenCall!|" in result


# ═══════════════════════════════════════════════════════════════════════════════
#  7. REST API
# ═══════════════════════════════════════════════════════════════════════════════

print("\n\033[1m=== REST API ===\033[0m")

@test("FastAPI app creates successfully")
def _():
    from gencall.api.routes import app
    assert app is not None
    assert app.title == "GenCall API"

@test("Health endpoint works")
def _():
    from fastapi.testclient import TestClient
    from gencall.core.config import Config
    from gencall.core.sipp_engine import SIPpEngine
    from gencall.core.stats import StatsEngine
    from gencall.scenarios.manager import ScenarioManager
    from gencall.api import routes

    Config.reset()
    config = Config()
    routes.engine = SIPpEngine(config)
    routes.stats = StatsEngine(config)
    routes.scenarios = ScenarioManager()
    routes.db = None

    client = TestClient(routes.app)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["name"] == "GenCall"
    assert data["version"] == "2.0.0"
    Config.reset()

@test("List scenarios endpoint")
def _():
    from fastapi.testclient import TestClient
    from gencall.api import routes
    client = TestClient(routes.app)
    resp = client.get("/api/scenarios")
    assert resp.status_code == 200
    data = resp.json()
    assert "scenarios" in data
    assert len(data["scenarios"]) >= 6

@test("Stats endpoint returns data")
def _():
    from fastapi.testclient import TestClient
    from gencall.api import routes
    client = TestClient(routes.app)
    resp = client.get("/api/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_calls" in data
    assert "calls_per_second" in data

@test("List tests endpoint")
def _():
    from fastapi.testclient import TestClient
    from gencall.api import routes
    client = TestClient(routes.app)
    resp = client.get("/api/tests")
    assert resp.status_code == 200
    assert "tests" in resp.json()

@test("Dashboard serves HTML")
def _():
    from fastapi.testclient import TestClient
    from gencall.api import routes
    from gencall.web.dashboard import router as dashboard_router
    # Mount dashboard like main.py does
    routes.app.include_router(dashboard_router)
    client = TestClient(routes.app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "GenCall" in resp.text


# ═══════════════════════════════════════════════════════════════════════════════
#  8. SRTP
# ═══════════════════════════════════════════════════════════════════════════════

print("\n\033[1m=== SRTP ===\033[0m")

@test("Generate crypto params")
def _():
    from gencall.core.srtp import CryptoParams, CryptoSuite
    params = CryptoParams.generate(CryptoSuite.AES_CM_128_HMAC_SHA1_80)
    assert len(params.master_key) == 16
    assert len(params.master_salt) == 14
    assert params.auth_tag_length == 10

@test("SDP crypto line round-trip")
def _():
    from gencall.core.srtp import CryptoParams, CryptoSuite
    params = CryptoParams.generate(CryptoSuite.AES_CM_128_HMAC_SHA1_80)
    sdp_line = params.to_sdp_line()
    assert "AES_CM_128_HMAC_SHA1_80" in sdp_line
    assert "inline:" in sdp_line
    parsed = CryptoParams.from_sdp_line(sdp_line)
    assert parsed is not None
    assert parsed.master_key == params.master_key
    assert parsed.master_salt == params.master_salt

@test("SRTP encrypt and decrypt round-trip")
def _():
    from gencall.core.srtp import CryptoParams, CryptoSuite, SRTPContext
    from gencall.core.rtp import rtp_header
    params = CryptoParams.generate(CryptoSuite.AES_CM_128_HMAC_SHA1_80)
    # Build a fake RTP packet
    header = rtp_header(2, 0, 0, 0, 8, 100, 160, 0x12345678)
    payload = b"\x80" * 160  # 160 bytes of audio
    rtp_packet = header + payload
    # Encrypt
    ctx_send = SRTPContext(params)
    srtp_packet = ctx_send.protect(rtp_packet)
    assert len(srtp_packet) == len(rtp_packet) + params.auth_tag_length
    assert srtp_packet != rtp_packet  # should be different
    # Decrypt
    ctx_recv = SRTPContext(params)
    decrypted = ctx_recv.unprotect(srtp_packet)
    assert decrypted is not None
    assert decrypted == rtp_packet  # should match original


# ═══════════════════════════════════════════════════════════════════════════════
#  9. OUTGOING CALL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

print("\n\033[1m=== Outgoing Call Helpers ===\033[0m")

@test("Codec negotiation from SDP")
def _():
    from gencall.scenarios.scripts.outgoing_call import negotiate_codec
    sdp = "m=audio 20000 RTP/AVP 8 0 101\r\na=rtpmap:8 PCMA/8000\r\n"
    codec = negotiate_codec(sdp)
    assert codec is not None
    assert codec.name == "PCMA"
    assert codec.payload_type == 8

@test("Codec negotiation - G.729 preferred")
def _():
    from gencall.scenarios.scripts.outgoing_call import negotiate_codec
    sdp = "m=audio 20000 RTP/AVP 18 8 0\r\n"
    codec = negotiate_codec(sdp)
    assert codec is not None
    assert codec.name == "G729"

@test("Traffic shaping probability check")
def _():
    from gencall.scenarios.scripts.outgoing_call import get_traffic_window
    window = get_traffic_window(14)  # 2 PM
    assert window.call_probability > 0.5  # afternoon should be busy
    night = get_traffic_window(3)  # 3 AM
    assert night.call_probability < 0.3  # night should be quiet

@test("Number pool from CSV")
def _():
    from gencall.scenarios.scripts.outgoing_call import NumberPool
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
        f.write("1001;2001\n1002;2002\n1003;2003\n")
        f.flush()
        pool = NumberPool(f.name)
        assert len(pool.callers) == 3
        assert len(pool.callees) == 3
        caller, callee = pool.random_pair()
        assert caller in ["1001", "1002", "1003"]
        assert callee in ["2001", "2002", "2003"]
    os.unlink(f.name)


# ═══════════════════════════════════════════════════════════════════════════════
#  10. INCOMING CALL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

print("\n\033[1m=== Incoming Call Helpers ===\033[0m")

@test("SIP URI parsing from From header")
def _():
    from gencall.scenarios.scripts.incoming_call import parse_sip_user, parse_sip_domain
    user = parse_sip_user('"John" <sip:john@10.0.0.1:5060>;tag=abc')
    assert user == "john"
    user = parse_sip_user("<sip:+15551234567@proxy.com>")
    assert user == "+15551234567"
    domain = parse_sip_domain("<sip:user@example.com:5060>;tag=x")
    assert domain == "example.com"


# ═══════════════════════════════════════════════════════════════════════════════
#  11. UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

print("\n\033[1m=== Utilities ===\033[0m")

@test("Password hashing and verification")
def _():
    from gencall.utils.auth import hash_password, verify_password
    hashed, salt = hash_password("mysecret")
    assert verify_password("mysecret", hashed, salt) == True
    assert verify_password("wrongpass", hashed, salt) == False

@test("API key generation")
def _():
    from gencall.utils.auth import generate_api_key
    key = generate_api_key()
    assert len(key) > 20
    key2 = generate_api_key()
    assert key != key2

@test("Network utility - get default IP")
def _():
    from gencall.utils.network import get_default_ip
    ip = get_default_ip()
    assert ip  # should return something


# ═══════════════════════════════════════════════════════════════════════════════
#  12. API GATEWAY
# ═══════════════════════════════════════════════════════════════════════════════

print("\n\033[1m=== API Gateway ===\033[0m")

@test("API key create and validate")
def _():
    from gencall.core.api_gateway import APIKeyManager
    mgr = APIKeyManager()
    raw_key, api_key = mgr.create_key("test-key")
    assert raw_key.startswith("gc_")
    validated = mgr.validate_key(raw_key)
    assert validated is not None
    assert validated.name == "test-key"
    assert mgr.validate_key("gc_bogus_key") is None

@test("API key revocation")
def _():
    from gencall.core.api_gateway import APIKeyManager
    mgr = APIKeyManager()
    raw_key, api_key = mgr.create_key("revoke-me")
    assert mgr.validate_key(raw_key) is not None
    mgr.revoke_key(api_key.key_id)
    assert mgr.validate_key(raw_key) is None

@test("Rate limiter")
def _():
    from gencall.core.api_gateway import RateLimiter
    limiter = RateLimiter()
    # Should allow up to limit
    for i in range(5):
        assert limiter.check("test", 5) == True
    # Should reject after limit
    assert limiter.check("test", 5) == False
    assert limiter.get_remaining("test", 5) == 0


# ═══════════════════════════════════════════════════════════════════════════════
#  13. API AUTHENTICATION
# ═══════════════════════════════════════════════════════════════════════════════

print("\n\033[1m=== API Authentication ===\033[0m")


def _auth_setup():
    """Wire routes with a DB-backed gateway and mint one key.

    Uses a temp *file* SQLite DB (not :memory:) so the TestClient's threadpool
    workers share the same database — matching how production runs on a file or
    Postgres. Returns (client, raw_key, key, gateway, db_path).
    """
    from fastapi.testclient import TestClient
    from gencall.core.config import Config
    from gencall.core.sipp_engine import SIPpEngine
    from gencall.core.stats import StatsEngine
    from gencall.scenarios.manager import ScenarioManager
    from gencall.db.models import Database
    from gencall.core.api_gateway import APIGateway, APIKeyManager
    from gencall.api import routes

    Config.reset()
    config = Config()
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(f"sqlite:///{db_path}")
    db.create_tables()

    routes.engine = SIPpEngine(config)
    routes.stats = StatsEngine(config)
    routes.scenarios = ScenarioManager()
    routes.db = db

    gateway = APIGateway()
    gateway.keys = APIKeyManager(db=db)
    routes.gateway = gateway
    raw_key, key = gateway.keys.create_key("test-admin")

    Config.reset()
    return TestClient(routes.app), raw_key, key, gateway, db_path


def _auth_teardown(db_path):
    from gencall.api import routes
    routes.gateway = None
    try:
        if routes.db is not None:
            routes.db.engine.dispose()
    except Exception:
        pass
    try:
        os.unlink(db_path)
    except OSError:
        pass


@test("Unauthenticated request is rejected with 401")
def _():
    client, _raw, _key, _gw, db_path = _auth_setup()
    try:
        resp = client.get("/api/tests")
        assert resp.status_code == 401, f"expected 401, got {resp.status_code}"
        # Health must remain public even with auth enabled
        assert client.get("/api/health").status_code == 200
    finally:
        _auth_teardown(db_path)


@test("Valid API key is accepted")
def _():
    client, raw_key, _key, _gw, db_path = _auth_setup()
    try:
        resp = client.get("/api/tests", headers={"X-API-Key": raw_key})
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}"
        assert "tests" in resp.json()
    finally:
        _auth_teardown(db_path)


@test("Revoked API key is rejected")
def _():
    client, raw_key, key, gw, db_path = _auth_setup()
    try:
        # Works before revocation
        assert client.get("/api/tests", headers={"X-API-Key": raw_key}).status_code == 200
        # Revoke and confirm it no longer works
        assert gw.keys.revoke_key(key.key_id) is True
        resp = client.get("/api/tests", headers={"X-API-Key": raw_key})
        assert resp.status_code == 401, f"expected 401 after revoke, got {resp.status_code}"
    finally:
        _auth_teardown(db_path)


@test("API key persists to the database")
def _():
    from gencall.db.models import Database, APIKey as APIKeyRow
    from gencall.core.api_gateway import APIKeyManager
    db = Database("sqlite:///")
    db.create_tables()
    mgr = APIKeyManager(db=db)
    raw_key, key = mgr.create_key("persisted")
    # A fresh manager on the same DB validates the same key
    mgr2 = APIKeyManager(db=db)
    validated = mgr2.validate_key(raw_key)
    assert validated is not None
    assert validated.name == "persisted"
    session = db.get_session()
    row = session.query(APIKeyRow).filter_by(key_id=key.key_id).first()
    assert row is not None and row.enabled is True
    session.close()


# ═══════════════════════════════════════════════════════════════════════════════
#  RESULTS
# ═══════════════════════════════════════════════════════════════════════════════

print(f"\n{'=' * 60}")
print(f"\033[1m  RESULTS: {PASS} passed, {FAIL} failed\033[0m")
print(f"{'=' * 60}")

if ERRORS:
    print("\n\033[91mFailed tests:\033[0m")
    for name, err in ERRORS:
        print(f"  - {name}: {err}")

print()
sys.exit(1 if FAIL > 0 else 0)
