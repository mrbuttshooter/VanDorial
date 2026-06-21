import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gencall.api import loops as loops_mod
from gencall.api.routes import require_api_key


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(loops_mod.router)
    app.dependency_overrides[require_api_key] = lambda: None
    return TestClient(app)


def test_traffic_calc_returns_schedule(client):
    r = client.post("/api/loops/traffic-calc",
                    json={"target_minutes": 1_000_000, "acd_s": 120, "profile": {}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["peak_cps"] > body["avg_cps"] > 0
    assert len(body["per_hour"]) == 24
    assert "peak_concurrent" in body


def test_traffic_calc_validates(client):
    assert client.post("/api/loops/traffic-calc",
                       json={"target_minutes": 1000, "acd_s": 0}).status_code == 422
