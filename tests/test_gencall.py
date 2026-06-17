"""
GenCall Test Suite

Tests core modules to verify they actually work:
  1. Config loading
  2. Database models + CRUD
  3. Scenario manager
  4. Stats engine
  5. REST API endpoints
  6. Utilities (auth, network)
  7. API gateway + authentication
"""

import os
import sys
import time
import json
import tempfile

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
    assert data["version"] == "2.0.3"
    Config.reset()

# Auth now fails CLOSED: protected endpoints need a wired gateway + a key. These
# three list/stats tests exercise endpoint logic, so we mint an in-memory key and
# send it. (The fail-open path they used to rely on no longer exists.)
def _authed_worker_client():
    import os
    import tempfile
    from fastapi.testclient import TestClient
    from gencall.api import routes
    from gencall.core.api_gateway import APIGateway, APIKeyManager
    from gencall.db.models import Database

    # A temp FILE sqlite DB (not :memory:) so the TestClient's threadpool sees the
    # same key the test thread minted — :memory: gives each connection its own DB.
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(f"sqlite:///{db_path}")
    db.create_tables()
    gateway = APIGateway()
    gateway.keys = APIKeyManager(db=db)
    raw_key, _ = gateway.keys.create_key("test-selfcheck")
    routes.gateway = gateway
    client = TestClient(routes.app)
    client.headers.update({"X-API-Key": raw_key})
    return client

@test("List scenarios endpoint")
def _():
    client = _authed_worker_client()
    resp = client.get("/api/scenarios")
    assert resp.status_code == 200
    data = resp.json()
    assert "scenarios" in data
    assert len(data["scenarios"]) >= 6

@test("Stats endpoint returns data")
def _():
    client = _authed_worker_client()
    resp = client.get("/api/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_calls" in data
    assert "calls_per_second" in data

@test("List tests endpoint")
def _():
    client = _authed_worker_client()
    resp = client.get("/api/tests")
    assert resp.status_code == 200
    assert "tests" in resp.json()

@test("Console-missing fallback page serves HTML")
def _():
    # The React console (web/console/) is the UI. When its build is absent the
    # server falls back to this minimal page at / instead of a legacy dashboard.
    from gencall.main import CONSOLE_MISSING_HTML
    assert "GenCall" in CONSOLE_MISSING_HTML
    assert "/api/health" in CONSOLE_MISSING_HTML

@test("Console bootstrap is unauthenticated and serves the key when set")
def _():
    # Any browser must reach /api/console/bootstrap WITHOUT a key (it is how a
    # fresh browser gets one). Returns the console key when this box serves the
    # console; 404 otherwise. We do NOT send an X-API-Key header here.
    from fastapi.testclient import TestClient
    from gencall.api import routes

    prev = routes.console_api_key
    try:
        routes.console_api_key = None
        client = TestClient(routes.app)
        assert client.get("/api/console/bootstrap").status_code == 404

        routes.console_api_key = "gc_test_console_key"
        resp = client.get("/api/console/bootstrap")
        assert resp.status_code == 200
        assert resp.json()["api_key"] == "gc_test_console_key"
    finally:
        routes.console_api_key = prev

@test("register_raw_key is idempotent and validates")
def _():
    from gencall.core.api_gateway import APIKeyManager
    mgr = APIKeyManager(db=None)
    k1 = mgr.register_raw_key("gc_fixed_console", name="console")
    k2 = mgr.register_raw_key("gc_fixed_console", name="console")
    assert k1.key_id == k2.key_id              # same hash => same record
    assert mgr.validate_key("gc_fixed_console") is not None
    assert mgr.validate_key("gc_wrong") is None


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
