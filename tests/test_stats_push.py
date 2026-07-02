"""
Worker→controller stats push (roadmap #9): the worker StatsPusher and the
aggregator's push-ingest + poll-fallback. No network — the sender and the node
client are stubbed.
"""

import asyncio


from gencall.controller.aggregator import FleetAggregator
from gencall.core.stats_push import StatsPusher, build_from_config


# ── worker StatsPusher ────────────────────────────────────────────────────────

class _Snap:
    def __init__(self, cps):
        self.cps = cps

    def to_dict(self):
        return {"calls_per_second": self.cps}


def test_submit_coalesces_to_latest_snapshot():
    p = StatsPusher("http://ctl:8090", "tok", "http://w:8080")
    # Three snapshots queued with no sender draining -> only the newest survives
    # (queue is size-1, coalescing).
    p.submit(_Snap(1))
    p.submit(_Snap(2))
    p.submit(_Snap(3))
    assert p._queue.qsize() == 1
    assert p._queue.get_nowait() == {"calls_per_second": 3}


def test_submit_never_raises_on_bad_snapshot():
    p = StatsPusher("http://ctl:8090", "tok", "http://w:8080")

    class Bad:
        def to_dict(self):
            raise RuntimeError("boom")

    p.submit(Bad())            # swallowed
    assert p._queue.qsize() == 0


def test_send_posts_address_stats_and_token(monkeypatch):
    p = StatsPusher("http://ctl:8090/", "s3cret", "http://w:8080")
    captured = {}

    class FakeResp:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = req.data
        return FakeResp()

    import gencall.core.stats_push as mod
    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    ok = p._send({"calls_per_second": 5})
    assert ok is True
    assert captured["url"] == "http://ctl:8090/api/fleet/ingest/stats"
    assert captured["headers"]["x-fleet-token"] == "s3cret"
    import json
    body = json.loads(captured["body"])
    assert body == {"address": "http://w:8080", "stats": {"calls_per_second": 5}}


def test_build_from_config_gated_on_controller_url():
    class Cfg:
        fleet_controller_url = ""
        fleet_token = "t"

    assert build_from_config(Cfg(), "http://w:8080") is None

    class Cfg2:
        fleet_controller_url = "http://ctl:8090"
        fleet_token = "t"

    p = build_from_config(Cfg2(), "http://w:8080")
    assert isinstance(p, StatsPusher)
    assert p.url == "http://ctl:8090/api/fleet/ingest/stats"
    assert p.address == "http://w:8080"


# ── aggregator ingest + poll fallback ─────────────────────────────────────────

class _StubClient:
    def __init__(self, stats):
        self._stats = stats

    async def get_stats(self):
        return dict(self._stats)


def _agg_with_one_node(poll_stats, freshness=15.0):
    node = {"id": 1, "address": "http://w:8080", "api_key": "k", "enabled": True}
    agg = FleetAggregator(lambda: [node], push_freshness_s=freshness)
    agg._client_for = lambda n: _StubClient(poll_stats)
    return agg


def test_ingest_pushed_stats_is_authoritative_over_poll():
    push = {"calls_per_second": 9, "current_calls": 3}
    poll = {"calls_per_second": 1, "current_calls": 0}
    agg = _agg_with_one_node(poll)
    agg.ingest_pushed_stats(1, push)
    # A poll tick must NOT clobber a fresh push.
    asyncio.run(agg._poll_stats_once())
    assert agg._node_stats[1] == push


def test_poll_resumes_as_fallback_when_push_goes_stale():
    push = {"calls_per_second": 9}
    poll = {"calls_per_second": 1}
    agg = _agg_with_one_node(poll, freshness=0.0)  # every push instantly stale
    agg.ingest_pushed_stats(1, push)
    asyncio.run(agg._poll_stats_once())
    # Freshness window is 0, so the poll overwrites with its own value.
    assert agg._node_stats[1] == poll


def test_pushed_at_dropped_when_node_leaves_inventory():
    agg = _agg_with_one_node({"calls_per_second": 1})
    agg.ingest_pushed_stats(1, {"calls_per_second": 9})
    # Node provider now returns no nodes -> node 1 is pruned from both maps.
    agg._node_provider = lambda: []
    asyncio.run(agg._poll_stats_once())
    assert 1 not in agg._node_stats
    assert 1 not in agg._pushed_at
