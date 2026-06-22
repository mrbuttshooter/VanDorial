"""Security headers present on every response (defense-in-depth)."""
from fastapi.testclient import TestClient
from gencall.api import routes


def test_security_headers_present():
    c = TestClient(routes.app)
    h = {k.lower(): v for k, v in c.get("/api/health").headers.items()}
    assert h.get("x-content-type-options") == "nosniff"
    assert h.get("x-frame-options") == "DENY"
    assert "frame-ancestors 'none'" in h.get("content-security-policy", "")
    assert h.get("referrer-policy") == "no-referrer"
