"""Console login: password hashing, user/session managers, and the HTTP flow.

Uses a file-backed SQLite DB (not ``:memory:``) because the FastAPI TestClient
runs endpoints in a worker thread, and a ``:memory:`` engine gives each thread a
separate empty database — a test-harness artifact, not a product bug.
"""

import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient

from gencall.db.models import Database
from gencall.core.api_gateway import APIGateway, APIKeyManager
from gencall.core.auth_users import (
    UserManager, SessionManager, hash_password, verify_password,
    SESSION_TOKEN_PREFIX,
)
from gencall.api import routes, auth


# ─── password hashing ────────────────────────────────────────────────────────

def test_password_hash_roundtrip_and_reject():
    h = hash_password("correct horse battery")
    assert h.startswith("pbkdf2_sha256$")
    assert verify_password("correct horse battery", h)
    assert not verify_password("wrong", h)
    # two hashes of the same password differ (random salt)
    assert h != hash_password("correct horse battery")


def test_verify_password_tolerates_garbage():
    for bad in ("", "nope", "a$b$c", None):
        assert not verify_password("x", bad)


# ─── managers ────────────────────────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path):
    d = Database(f"sqlite:///{tmp_path/'auth.db'}")
    d.create_tables()
    return d


def test_user_manager_crud(db):
    um = UserManager(db)
    assert um.count_users() == 0
    u = um.create_user("alice", "supersecret")
    assert um.count_users() == 1
    # duplicate rejected
    with pytest.raises(ValueError):
        um.create_user("alice", "supersecret2")
    # short password rejected
    with pytest.raises(ValueError):
        um.create_user("bob", "short")
    # verify
    assert um.verify("alice", "supersecret")["username"] == "alice"
    assert um.verify("alice", "bad") is None
    assert um.verify("ghost", "x") is None
    # set password
    assert um.set_password(u["id"], "newpassword1")
    assert um.verify("alice", "newpassword1")
    assert not um.verify("alice", "supersecret")
    # delete
    assert um.delete_user(u["id"])
    assert um.count_users() == 0


def test_session_manager_lifecycle(db):
    um = UserManager(db)
    sm = SessionManager(db)
    u = um.create_user("alice", "supersecret")
    tok, exp = sm.create(u["id"], "alice")
    assert tok.startswith(SESSION_TOKEN_PREFIX)
    assert sm.validate(tok)["username"] == "alice"
    assert sm.validate("gcs_bogus") is None
    assert sm.revoke(tok)
    assert sm.validate(tok) is None


def test_session_expiry(db):
    um = UserManager(db)
    sm = SessionManager(db, ttl_s=-1)  # already expired on creation
    u = um.create_user("alice", "supersecret")
    tok, _ = sm.create(u["id"], "alice")
    assert sm.validate(tok) is None  # expired -> rejected (and purged)


# ─── HTTP flow ───────────────────────────────────────────────────────────────

@pytest.fixture()
def client(db, monkeypatch):
    gw = APIGateway()
    gw.keys = APIKeyManager(db=db)
    gw.users = UserManager(db=db)
    gw.sessions = SessionManager(db=db)
    monkeypatch.setattr(routes, "gateway", gw)
    auth._fail_log.clear()  # reset the per-IP throttle between tests
    gw.users.create_user("admin", "supersecret", role="admin")

    app = FastAPI()
    app.include_router(auth.router)

    @app.get("/api/protected", dependencies=[Depends(routes.require_api_key)])
    def protected():
        return {"ok": True}

    @app.post("/api/protected", dependencies=[Depends(routes.require_api_key)])
    def protected_write():
        return {"ok": True}

    return TestClient(app), gw


def test_login_and_session_auth(client):
    c, _ = client
    assert c.get("/api/protected").status_code == 401
    assert c.post("/api/auth/login",
                  json={"username": "admin", "password": "wrong"}).status_code == 401
    r = c.post("/api/auth/login", json={"username": "admin", "password": "supersecret"})
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    assert c.get("/api/protected", headers={"X-API-Key": tok}).status_code == 200
    assert c.get("/api/auth/me", headers={"X-API-Key": tok}).json()["username"] == "admin"
    # logout revokes
    assert c.post("/api/auth/logout", headers={"X-API-Key": tok}).status_code == 200
    assert c.get("/api/protected", headers={"X-API-Key": tok}).status_code == 401


def test_login_throttle(client):
    c, _ = client
    for _ in range(5):
        assert c.post("/api/auth/login",
                      json={"username": "admin", "password": "x"}).status_code == 401
    # 6th attempt within the window is throttled even with the right password
    assert c.post("/api/auth/login",
                  json={"username": "admin", "password": "supersecret"}).status_code == 429


def test_machine_api_key_still_works(client):
    c, gw = client
    raw, _ = gw.keys.create_key("ci")
    assert c.get("/api/protected", headers={"X-API-Key": raw}).status_code == 200


def test_user_management_endpoints(client):
    c, _ = client
    tok = c.post("/api/auth/login",
                 json={"username": "admin", "password": "supersecret"}).json()["token"]
    h = {"X-API-Key": tok}
    # create a second user
    r = c.post("/api/auth/users", json={"username": "bob", "password": "bobsecret1"}, headers=h)
    assert r.status_code == 200, r.text
    bob_id = r.json()["user"]["id"]
    assert len(c.get("/api/auth/users", headers=h).json()["users"]) == 2
    # the new user can log in
    assert c.post("/api/auth/login",
                  json={"username": "bob", "password": "bobsecret1"}).status_code == 200
    # delete bob
    assert c.delete(f"/api/auth/users/{bob_id}", headers=h).status_code == 200
    # cannot delete the last remaining user
    admin_id = c.get("/api/auth/users", headers=h).json()["users"][0]["id"]
    assert c.delete(f"/api/auth/users/{admin_id}", headers=h).status_code == 409


def test_user_endpoints_require_auth(client):
    c, _ = client
    assert c.get("/api/auth/users").status_code == 401
    assert c.post("/api/auth/users",
                  json={"username": "x", "password": "yyyyyyyy"}).status_code == 401


def test_bootstrap_disabled_once_a_user_exists(db, monkeypatch):
    """Legacy auto-auth hands out a key only while there are zero accounts; once
    an admin exists it 404s so the console must log in (migration safety)."""
    from fastapi import HTTPException
    gw = APIGateway()
    gw.keys = APIKeyManager(db=db)
    gw.users = UserManager(db=db)
    gw.sessions = SessionManager(db=db)
    monkeypatch.setattr(routes, "gateway", gw)
    monkeypatch.setattr(routes, "console_api_key", "gc_demo_key")

    import types
    local = types.SimpleNamespace(client=types.SimpleNamespace(host="127.0.0.1"))
    remote = types.SimpleNamespace(client=types.SimpleNamespace(host="10.0.0.9"))

    # zero users + loopback -> key (don't lock out a LOCAL upgrade)
    assert routes.console_bootstrap(local) == {"api_key": "gc_demo_key"}
    # zero users + network caller -> refused (never hand admin to the network)
    with pytest.raises(HTTPException) as ei:
        routes.console_bootstrap(remote)
    assert ei.value.status_code == 404

    # once a user exists -> bootstrap is disabled for everyone
    gw.users.create_user("admin", "supersecret")
    with pytest.raises(HTTPException) as ei:
        routes.console_bootstrap(local)
    assert ei.value.status_code == 404


def _login(c, username, password):
    return c.post("/api/auth/login",
                  json={"username": username, "password": password}).json()["token"]


def test_viewer_is_read_only(client):
    """A viewer may GET but not issue any state-changing method."""
    c, gw = client
    gw.users.create_user("val", "viewersecret", role="viewer")
    tok = _login(c, "val", "viewersecret")
    h = {"X-API-Key": tok}
    # Reads work, and identity reports the role + can_write=False.
    assert c.get("/api/protected", headers=h).status_code == 200
    me = c.get("/api/auth/me", headers=h).json()
    assert me["role"] == "viewer" and me["can_write"] is False
    # A write on the generic protected route is blocked with 403 (not 401).
    assert c.post("/api/protected", headers=h).status_code == 403


def test_operator_can_write_but_not_manage_users(client):
    c, gw = client
    gw.users.create_user("opp", "operatorsecret", role="operator")
    tok = _login(c, "opp", "operatorsecret")
    h = {"X-API-Key": tok}
    me = c.get("/api/auth/me", headers=h).json()
    assert me["role"] == "operator" and me["can_write"] is True
    # Writes on operational routes are allowed...
    assert c.post("/api/protected", headers=h).status_code == 200
    # ...but account management is admin-only.
    assert c.get("/api/auth/users", headers=h).status_code == 403
    assert c.post("/api/auth/users",
                  json={"username": "x", "password": "yyyyyyyy"},
                  headers=h).status_code == 403


def test_admin_can_create_roled_users(client):
    c, _ = client
    tok = _login(c, "admin", "supersecret")
    h = {"X-API-Key": tok}
    r = c.post("/api/auth/users",
               json={"username": " viewerbob", "password": "bobsecret1",
                     "role": "viewer"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["user"]["role"] == "viewer"
    # An invalid role is rejected.
    assert c.post("/api/auth/users",
                  json={"username": "nope", "password": "nopesecret",
                        "role": "superuser"}, headers=h).status_code == 422


def test_role_change_takes_effect_next_request(client):
    """Demoting a live session to viewer blocks its next write (role is read
    from the user row per request, not frozen at login)."""
    c, gw = client
    user = gw.users.create_user("dyn", "dynsecret1", role="operator")
    tok = _login(c, "dyn", "dynsecret1")
    h = {"X-API-Key": tok}
    assert c.post("/api/protected", headers=h).status_code == 200
    gw.users.set_role(user["id"], "viewer")
    assert c.post("/api/protected", headers=h).status_code == 403
    assert c.get("/api/protected", headers=h).status_code == 200


def test_machine_key_is_full_access(client):
    c, gw = client
    raw, _ = gw.keys.create_key("ci")
    h = {"X-API-Key": raw}
    assert c.post("/api/protected", headers=h).status_code == 200
    # Machine keys may manage users (trusted automation).
    assert c.get("/api/auth/users", headers=h).status_code == 200
