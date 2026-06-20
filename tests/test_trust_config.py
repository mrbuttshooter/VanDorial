"""
Worker runtime trust-config endpoints (design §5.3, controller push target).

GET/POST /api/config/trust hot-apply the inbound trust whitelist + drop flag on
the running CallRecordParser (the same thread-safe setter the controller fans
out to). Empty ips = allow-all (existing semantics). No real SIPp/DB needed —
the parser runs db=None and we mount only the loops router with auth disabled.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gencall.api import loops as loops_mod
from gencall.api import routes as routes_mod  # noqa: F401  (require_api_key lives here)
from gencall.core.call_records import CallRecordParser


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.dependency_overrides = {}
    parser = CallRecordParser(db=None, trust_whitelist=[], drop_untrusted=False)
    loops_mod.call_parser = parser
    app.include_router(loops_mod.router)
    from gencall.api.routes import require_api_key
    app.dependency_overrides[require_api_key] = lambda: None
    return TestClient(app), parser


def test_get_then_post_trust(client):
    c, parser = client
    assert c.get("/api/config/trust").json() == {"ips": [], "drop_untrusted": False}
    r = c.post("/api/config/trust", json={"ips": ["10.0.0.1", "192.168.0.0/24"], "drop_untrusted": True})
    assert r.status_code == 200, r.text
    assert parser.get_trust() == {"ips": ["10.0.0.1", "192.168.0.0/24"], "drop_untrusted": True}
    assert c.get("/api/config/trust").json()["drop_untrusted"] is True


def test_post_trust_rejects_bad_ip(client):
    c, _ = client
    assert c.post("/api/config/trust", json={"ips": ["not-an-ip"], "drop_untrusted": False}).status_code == 422
