"""Auto tz_offset wiring on the loop run path (v2.2.3).

A profiled campaign started on a node picks up that node's drop-zone timezone
automatically, so the diurnal curve lands on the destination market's local
daypart without the operator typing an offset. The resolution happens in
``_start_on_member`` (the single chokepoint for single-node, group, and remote
preset runs).
"""
import pytest

from gencall.api import loops as loops_api


@pytest.fixture
def started_kwargs(monkeypatch):
    """Capture the kwargs the run path hands to LoopEngine.start_campaign."""
    rec = {}

    class FakeEngine:
        def start_campaign(self, **kw):
            rec.update(kw)
            return {"id": "loop-1"}

    monkeypatch.setattr(loops_api, "loop_engine", FakeEngine())
    return rec


def _member(**over):
    m = {"id": 7, "name": "n1", "ip": "10.0.0.1", "csv_path": "/pools/p.csv",
         "enabled": True, "dest_zone": "Iraq"}
    m.update(over)
    return m


def test_profiled_run_auto_derives_tz_from_drop_zone(started_kwargs):
    params = {"dest_host": "203.0.113.10", "rate": 1.0,
              "profile_enabled": True, "tz_offset": 0}
    res = loops_api._start_on_member("camp", params, _member(dest_zone="Iraq"))
    assert res["ok"] is True
    assert started_kwargs["tz_offset"] == 3        # auto from Iraq, not the manual 0
    assert params["tz_offset"] == 0                # caller dict untouched (group reuse)


def test_unknown_drop_country_keeps_manual_tz(started_kwargs):
    params = {"dest_host": "203.0.113.10", "rate": 1.0,
              "profile_enabled": True, "tz_offset": 5}
    loops_api._start_on_member("camp", params, _member(dest_zone="Atlantis"))
    assert started_kwargs["tz_offset"] == 5        # falls back to the preset's value


def test_non_profiled_run_does_not_touch_tz(started_kwargs):
    params = {"dest_host": "203.0.113.10", "rate": 1.0,
              "profile_enabled": False, "tz_offset": 0}
    loops_api._start_on_member("camp", params, _member(dest_zone="Iraq"))
    assert started_kwargs["tz_offset"] == 0        # non-profiled runs unchanged
