"""Phase 2 traffic-shaper tests (design: diurnal shaping, plan 2026-06-20).

Three layers, each a separate task:

  * Task 5 — the diurnal profile persists on a LoopPreset (model round-trip) and
    is carried on a running campaign (start_campaign passthrough + DB row).
  * Task 6 — LoopEngine.step_campaign_rate() relaunches a campaign's UAC at a new
    rate with NO traffic dip (overlap relaunch): a fresh UAC starts at the new
    rate, then the old one is drained/removed.
  * Task 7 — the shaper computes the right per-hour rate from the campaign's
    profile, and the shaper thread is idle-safe.

Engine tests reuse the conftest ``stub_sipp`` fixture (Config repointed at the
cross-platform fake sipp) + a real SIPpEngine + sqlite Database, mirroring
tests/test_loop_engine.py. The real LoopEngine signature is
``LoopEngine(sipp_engine, db=None, config=None)`` — the first positional is the
SIPpEngine, so we build one over the stub rather than passing config alone.
"""

import time

import pytest

from gencall.core.loop_engine import LoopEngine
from gencall.core.process_registry import ProcessRegistry
from gencall.core.sipp_engine import SIPpEngine, SIPpState
from gencall.db.migrations import apply_migrations
from gencall.db.models import Database, LoopPreset


# ─── Fixtures (mirror tests/test_loop_engine.py) ──────────────────────────────

@pytest.fixture
def db(tmp_path):
    """A real sqlite Database with the SQL migrations applied (loop_campaigns +
    its profile columns)."""
    database = Database(f"sqlite:///{tmp_path / 'shaper.db'}")
    database.create_tables()
    apply_migrations(database.engine)
    return database


@pytest.fixture
def loop_engine(stub_sipp, db):
    """A LoopEngine over a real SIPpEngine wired to a ProcessRegistry + DB.

    ``stub_sipp`` has reset Config and repointed sipp_command at the fake sipp,
    so SIPpEngine.start_instance launches the stub.
    """
    config = stub_sipp.config
    registry = ProcessRegistry(db=db)
    engine = SIPpEngine(config=config, registry=registry)
    le = LoopEngine(engine, db=db, config=config)
    yield le
    # Tear down: stop both threads + every spawned process so no stub outlives.
    le.stop_monitor()
    le.stop_shaper()
    engine.stop_all()


def _wait_running(engine, instance_id, timeout=5.0):
    """Poll until the named instance reaches RUNNING (the stub starts fast)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        inst = engine.get_instance(instance_id)
        if inst is not None and inst.state == SIPpState.RUNNING:
            return inst
        time.sleep(0.05)
    return engine.get_instance(instance_id)


# ─── Task 5: profile persistence ──────────────────────────────────────────────

def test_loop_preset_profile_columns_roundtrip(tmp_path):
    """The eight diurnal-profile fields persist on a LoopPreset and round-trip
    through to_dict (the columns the create-preset handler writes)."""
    db = Database(f"sqlite:///{tmp_path / 'p.db'}")
    db.create_tables()
    s = db.get_session()
    try:
        s.add(LoopPreset(name="diurnal-1", profile_enabled=True,
                         profile_preset="diurnal", night_floor=0.3,
                         ramp_up_start=6, plateau_start=9, plateau_end=18,
                         ramp_down_end=22, tz_offset=3, target_minutes=1000000))
        s.commit()
        row = s.query(LoopPreset).one()
        d = row.to_dict()
        assert d["profile_enabled"] is True and d["night_floor"] == 0.3
        assert d["tz_offset"] == 3 and d["target_minutes"] == 1000000
        assert d["profile_preset"] == "diurnal"
    finally:
        s.close()


def test_start_campaign_carries_and_persists_profile(loop_engine, db):
    """start_campaign accepts the profile kwargs, stores them on the campaign
    dict, and persists them to the loop_campaigns row."""
    c = loop_engine.start_campaign(
        dest_host="203.0.113.10", rate=1.0, max_concurrent=50, duration_s=10,
        local_ip="", profile_enabled=True, profile_preset="diurnal",
        night_floor=0.3, tz_offset=3, target_minutes=1_000_000,
    )
    cid = c["id"]
    # The in-memory campaign dict carries the profile.
    camp = loop_engine._campaigns[cid]
    assert camp["profile_enabled"] is True
    assert camp["night_floor"] == 0.3 and camp["tz_offset"] == 3
    assert camp["target_minutes"] == 1_000_000
    # The public campaign (API view) carries it too.
    assert c["profile_enabled"] is True and c["night_floor"] == 0.3

    # And it round-trips through the DB row.
    from sqlalchemy import text
    with db.engine.connect() as conn:
        row = conn.execute(
            text("SELECT profile_enabled, night_floor, tz_offset, target_minutes "
                 "FROM loop_campaigns WHERE id = :id"), {"id": cid}
        ).fetchone()
    assert row is not None
    assert bool(row[0]) is True and float(row[1]) == 0.3
    assert int(row[2]) == 3 and int(row[3]) == 1_000_000
    loop_engine.stop_campaign(cid)


# ─── Task 6: overlap-relaunch (step a campaign's rate, no dip) ─────────────────

def test_step_campaign_rate_overlap_relaunch(loop_engine):
    """step_campaign_rate starts a fresh UAC at the new rate, then drains the old
    one — a new instance id replaces the old, at the new rate."""
    eng = loop_engine
    c = eng.start_campaign(dest_host="203.0.113.10", rate=2.0, max_concurrent=50,
                           duration_s=10, local_ip="")
    cid = c["id"]
    old_iid = eng._campaigns[cid]["instance_id"]
    _wait_running(eng.engine, old_iid)

    assert eng.step_campaign_rate(cid, 5.0) is True
    new_iid = eng._campaigns[cid]["instance_id"]
    assert new_iid != old_iid
    inst = eng.engine.get_instance(new_iid)
    assert inst is not None and inst.call_rate == 5.0
    assert eng._campaigns[cid]["rate"] == 5.0

    # The old instance is stopped/removed (drained outside the lock).
    old = eng.engine.get_instance(old_iid)
    assert old is None or old.state.value in ("stopped", "stopping", "error")
    eng.stop_campaign(cid)


def test_step_campaign_rate_two_uacs_same_ip_distinct_source_ports(loop_engine):
    """The relaunch's verify step: during overlap two UACs for the same campaign
    briefly share one local_ip. The new UAC must start without a source-port
    collision. SIP source port is -p 0 (OS ephemeral) and each UAC gets a unique
    -mp media port, so the second instance starts cleanly even on the same IP."""
    eng = loop_engine
    c = eng.start_campaign(dest_host="203.0.113.10", rate=2.0, max_concurrent=50,
                           duration_s=10, local_ip="10.20.30.40")
    cid = c["id"]
    old_iid = eng._campaigns[cid]["instance_id"]
    old = _wait_running(eng.engine, old_iid)
    assert old.local_ip == "10.20.30.40"
    old_media = old.media_port

    assert eng.step_campaign_rate(cid, 4.0) is True
    new_iid = eng._campaigns[cid]["instance_id"]
    new = _wait_running(eng.engine, new_iid)
    # Same source IP carried over, started without collision.
    assert new is not None and new.state == SIPpState.RUNNING
    assert new.local_ip == "10.20.30.40"
    # Both used -p 0 (OS-assigned ephemeral) for the SIP signalling port.
    assert old.local_port == 0 and new.local_port == 0
    # Distinct -mp media ports (the engine allocates a unique one per instance).
    assert new.media_port != old_media
    eng.stop_campaign(cid)


def test_step_campaign_rate_rejects_unchanged_and_bad_rate(loop_engine):
    """No relaunch for an unchanged rate, a non-positive rate, or an over-cap
    rate — step returns False and the instance id is unchanged."""
    eng = loop_engine
    c = eng.start_campaign(dest_host="203.0.113.10", rate=3.0, max_concurrent=10,
                           duration_s=10, local_ip="")
    cid = c["id"]
    iid = eng._campaigns[cid]["instance_id"]
    _wait_running(eng.engine, iid)

    assert eng.step_campaign_rate(cid, 3.0) is False          # unchanged
    assert eng.step_campaign_rate(cid, 0) is False            # non-positive
    assert eng.step_campaign_rate(cid, -1.0) is False         # negative
    over = eng.config.loops_max_rate_cps + 1
    assert eng.step_campaign_rate(cid, over) is False         # over the cap
    assert eng._campaigns[cid]["instance_id"] == iid          # never relaunched
    eng.stop_campaign(cid)


def test_step_campaign_rate_unknown_or_stopped_returns_false(loop_engine):
    """A non-existent or already-stopped campaign can't be stepped."""
    eng = loop_engine
    assert eng.step_campaign_rate("loop-does-not-exist", 5.0) is False
    c = eng.start_campaign(dest_host="203.0.113.10", rate=2.0, duration_s=10,
                           local_ip="")
    cid = c["id"]
    _wait_running(eng.engine, eng._campaigns[cid]["instance_id"])
    eng.stop_campaign(cid)
    assert eng.step_campaign_rate(cid, 5.0) is False          # status != running


# ─── Task 7: shaper thread (hourly step along the curve) ───────────────────────

def test_shaper_computes_step_rate_for_hour(loop_engine):
    """With an injected hour, the shaper computes the curve's per-hour CPS for a
    profiled campaign (ACD == duration_s, identical to the Calculator)."""
    from gencall.core import traffic_profile
    eng = loop_engine
    c = eng.start_campaign(dest_host="203.0.113.10", rate=1.0, max_concurrent=200,
                           duration_s=120, local_ip="",
                           profile_enabled=True, target_minutes=1_000_000,
                           night_floor=0.25)
    cid = c["id"]
    expected = traffic_profile.calculate(
        1_000_000, 120, {"night_floor": 0.25})["per_hour"][14]["cps"]
    rate14 = eng._shaper_target_rate(eng._campaigns[cid], hour=14)
    assert abs(rate14 - expected) < 1e-6
    # A night hour is lower than a plateau hour (the curve actually bends).
    rate2 = eng._shaper_target_rate(eng._campaigns[cid], hour=2)
    assert rate2 < rate14
    eng.stop_campaign(cid)


def test_shaper_target_rate_clamped_to_cap(loop_engine, monkeypatch):
    """The per-hour rate is clamped to the per-campaign rate cap."""
    eng = loop_engine
    monkeypatch.setattr(type(eng.config), "loops_max_rate_cps",
                        property(lambda self: 1.0))
    c = eng.start_campaign(dest_host="203.0.113.10", rate=0.5, max_concurrent=200,
                           duration_s=120, local_ip="",
                           profile_enabled=True, target_minutes=1_000_000)
    cid = c["id"]
    # Peak would be ~9 cps; the 1.0 cap clamps it.
    assert eng._shaper_target_rate(eng._campaigns[cid], hour=14) == 1.0
    eng.stop_campaign(cid)


def test_shaper_thread_starts_for_profiled_campaign_and_stops_clean(loop_engine):
    """start_campaign(profile_enabled=True) launches the shaper thread; it is
    daemon + event-driven (idle-safe) and stops cleanly."""
    eng = loop_engine
    c = eng.start_campaign(dest_host="203.0.113.10", rate=1.0, max_concurrent=200,
                           duration_s=120, local_ip="",
                           profile_enabled=True, target_minutes=1_000_000)
    cid = c["id"]
    assert eng._shaper_thread is not None and eng._shaper_thread.is_alive()
    assert eng._shaper_thread.daemon is True
    eng.stop_shaper()
    assert eng._shaper_thread is None
    eng.stop_campaign(cid)


def test_shaper_thread_not_started_without_profile(loop_engine):
    """A plain (non-profiled) campaign does not spin up the shaper thread."""
    eng = loop_engine
    c = eng.start_campaign(dest_host="203.0.113.10", rate=1.0, duration_s=10,
                           local_ip="")
    assert eng._shaper_thread is None
    eng.stop_campaign(c["id"])


def test_shaper_sets_initial_rate_to_current_hour(loop_engine):
    """A profiled campaign starts at the current hour's curve value (not the
    request's nominal rate), so it reads as organic from the first minute."""
    import time as _time
    eng = loop_engine
    hour = _time.gmtime().tm_hour
    c = eng.start_campaign(dest_host="203.0.113.10", rate=1.0, max_concurrent=200,
                           duration_s=120, local_ip="",
                           profile_enabled=True, target_minutes=1_000_000,
                           night_floor=0.25)
    cid = c["id"]
    expected = eng._shaper_target_rate(eng._campaigns[cid], hour=hour)
    # The running UAC is at the hour's curve rate (overlap-relaunched at start),
    # not the nominal 1.0 from the request.
    iid = eng._campaigns[cid]["instance_id"]
    _wait_running(eng.engine, iid)
    assert abs(eng._campaigns[cid]["rate"] - expected) < 1e-6
    inst = eng.engine.get_instance(iid)
    assert abs(inst.call_rate - expected) < 1e-6
    eng.stop_campaign(cid)


def test_shaper_initial_rate_anchored_to_gmt_not_local(loop_engine, monkeypatch):
    """The diurnal curve follows GMT, not the box's local clock: when UTC hour
    and local hour differ, a profiled campaign starts at the UTC hour's rate.
    (Regression for "the trend takes effect on box-local time, not GMT".)"""
    import time as _t
    eng = loop_engine
    tmpl = _t.gmtime(0)  # a valid struct_time to clone wday/yday from

    def _at(h):
        return _t.struct_time((2026, 6, 23, h, 0, 0, tmpl.tm_wday, tmpl.tm_yday, 0))

    monkeypatch.setattr(_t, "gmtime", lambda *a: _at(2))       # UTC 02:00 (night trough)
    monkeypatch.setattr(_t, "localtime", lambda *a: _at(14))   # local 14:00 (plateau peak)

    c = eng.start_campaign(dest_host="203.0.113.10", rate=1.0, max_concurrent=200,
                           duration_s=120, local_ip="",
                           profile_enabled=True, target_minutes=1_000_000,
                           night_floor=0.25)
    cid = c["id"]
    gmt_rate = eng._shaper_target_rate(eng._campaigns[cid], hour=2)
    local_rate = eng._shaper_target_rate(eng._campaigns[cid], hour=14)
    assert gmt_rate != local_rate  # sanity: the curve bends between these hours
    iid = eng._campaigns[cid]["instance_id"]
    _wait_running(eng.engine, iid)
    # Initial UAC rate must match the GMT (02:00) curve value, not local (14:00).
    assert abs(eng._campaigns[cid]["rate"] - gmt_rate) < 1e-6
    eng.stop_campaign(cid)


def test_resume_preserves_diurnal_profile(loop_engine, db):
    """Regression: a profiled campaign that was running before a restart resumes
    STILL profiled. resume_interrupted used to omit the profile columns, so every
    restart/deploy silently re-launched loops flat (profile_enabled defaulted to
    False)."""
    from sqlalchemy import text
    with db.engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO loop_campaigns "
            "(id,name,status,dest_host,dest_port,rate,duration_s,target_minutes,"
            " profile_enabled,night_floor,tz_offset,created_at) VALUES "
            "('old-1','SA','running','203.0.113.10',5060,1.0,120,1000000,"
            " 1,0.25,2,'2026-01-01T00:00:00+00:00')"))
    eng = loop_engine
    eng._resolve_resume_csv = lambda c: "/tmp/resume_test.csv"   # pretend a pool exists
    rec = {}
    eng.start_campaign = lambda **kw: (rec.update(kw) or {"id": "new-1"})
    eng.resume_interrupted()
    # The resumed campaign carries the diurnal profile (not flattened).
    assert rec.get("profile_enabled") is True
    assert rec.get("target_minutes") == 1_000_000
    assert rec.get("tz_offset") == 2
    assert abs(rec.get("night_floor", 0) - 0.25) < 1e-9
