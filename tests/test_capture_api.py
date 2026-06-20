import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gencall.api import loops as loops_mod
from gencall.api.routes import require_api_key


class FakeManager:
    def __init__(self):
        self._n = 0
        self.caps = {}

    def start(self, campaign_id, bpf, iface="any"):
        self._n += 1
        cid = f"c{self._n}"
        self.caps[cid] = {"id": cid, "campaign_id": campaign_id, "running": True,
                          "size_bytes": 4, "started_at": 1.0, "stopped_at": None}
        return self.caps[cid]

    def stop(self, cid):
        self.caps[cid]["running"] = False
        return self.caps[cid]

    def list(self, campaign_id=None):
        return [c for c in self.caps.values() if campaign_id in (None, c["campaign_id"])]

    def delete(self, cid):
        self.caps.pop(cid, None)

    def path(self, cid):
        return __file__  # any readable file for the download test


class FakeEngine:
    def get_campaign(self, cid):
        if cid != "loop-x":
            raise KeyError(cid)
        return {"id": cid, "dest_host": "203.0.113.10", "dest_port": 5060,
                "transport": "udp",
                "sipp": {"local_port": 5071, "media_port": 10000}}


@pytest.fixture
def client():
    loops_mod.loop_engine = FakeEngine()
    loops_mod.capture_manager = FakeManager()
    app = FastAPI()
    app.include_router(loops_mod.router)
    app.dependency_overrides[require_api_key] = lambda: None
    return TestClient(app)


def test_capture_lifecycle(client):
    r = client.post("/api/loops/loop-x/capture/start")
    assert r.status_code == 200, r.text
    cid = r.json()["capture"]["id"]
    assert any(c["id"] == cid for c in client.get("/api/loops/loop-x/captures").json()["captures"])
    assert client.post(f"/api/loops/loop-x/capture/{cid}/stop").status_code == 200
    dl = client.get(f"/api/loops/loop-x/capture/{cid}/download")
    assert dl.status_code == 200 and len(dl.content) > 0
    assert client.delete(f"/api/loops/loop-x/capture/{cid}").status_code == 200


def test_capture_start_unknown_campaign_404(client):
    assert client.post("/api/loops/nope/capture/start").status_code == 404
