"""
LoopEngine + loops API tests against the stub `sipp` (design §4.1 / §4.4 / §6).

Runs WITHOUT real SIPp/Docker/Linux: the conftest ``stub_sipp`` fixture points
``config.sipp_command`` at the cross-platform fake sipp (tests/stubs/fake_sipp.py)
and resets the Config singleton. We build a real SIPpEngine + ProcessRegistry +
sqlite Database + LoopEngine and exercise:

  * start a campaign     -> UAC spawned (PID registered) + a 'running' DB row;
  * stop a campaign      -> UAC process gone, status 'stopped';
  * caps enforced        -> an over-limit start is refused (CapExceeded / 409);
  * answer status        -> the persistent UAS reports running.

Both the engine surface and the FastAPI router (via TestClient) are covered.
"""

import time

import pytest

from gencall.core.config import Config
from gencall.core.loop_engine import CapExceeded, IPBusy, LoopEngine, UAS_INSTANCE_ID
from gencall.core.process_registry import ProcessRegistry
from gencall.core.sipp_engine import SIPpEngine, SIPpState
from gencall.db.migrations import apply_migrations
from gencall.db.models import Database


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    """A real sqlite Database with the SQL migrations applied (loop_campaigns,
    call_records, managed_processes)."""
    database = Database(f"sqlite:///{tmp_path / 'loop.db'}")
    database.create_tables()
    apply_migrations(database.engine)
    return database


@pytest.fixture
def loop_engine(stub_sipp, db):
    """A LoopEngine over a real SIPpEngine wired to a ProcessRegistry + DB.

    ``stub_sipp`` has already reset Config and repointed sipp_command at the
    stub, so SIPpEngine.start_instance launches the fake sipp.
    """
    config = stub_sipp.config
    registry = ProcessRegistry(db=db)
    engine = SIPpEngine(config=config, registry=registry)
    le = LoopEngine(engine, db=db, config=config)
    yield le
    # Tear down: stop monitor + every spawned process so no stub outlives a test.
    le.stop_monitor()
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


# ─── Engine-level tests ───────────────────────────────────────────────────────

def test_start_campaign_spawns_uac_registers_pid_and_db_row(loop_engine, db):
    """Starting a campaign spawns a UAC, registers its PID, and writes a row."""
    campaign = loop_engine.start_campaign(
        name="c1", dest_host="1.2.3.4", rate=5.0, max_concurrent=4,
        duration_s=1, target_calls=0,
    )
    cid = campaign["id"]
    assert campaign["status"] == "running"

    inst = _wait_running(loop_engine.engine, f"uac-{cid}")
    assert inst is not None
    assert inst.state == SIPpState.RUNNING

    # PID registered in managed_processes for crash-orphan reconciliation.
    pids = [r["pid"] for r in ProcessRegistry(db=db).list_all()]
    assert inst._process.pid in pids

    # A 'running' DB row exists.
    from sqlalchemy import text
    with db.engine.connect() as conn:
        row = conn.execute(
            text("SELECT status FROM loop_campaigns WHERE id = :id"), {"id": cid}
        ).fetchone()
    assert row is not None
    assert row[0] == "running"


def test_resume_interrupted_relaunches_and_marks_old_terminal(loop_engine, db, tmp_path):
    """After a restart, resume_interrupted re-launches a campaign left in the DB
    as 'interrupted' and marks the OLD row terminal so it can't be resumed twice.

    Simulates the "restart .4 and loops come back" path: a prior boot's campaign
    row exists with status interrupted and a still-present pool CSV; on resume a
    fresh running campaign starts with the same params and the old row goes
    'stopped'.
    """
    from sqlalchemy import text

    # A pool CSV that still exists (service restart keeps /tmp).
    csv = tmp_path / "pool.csv"
    csv.write_text("100,224600111111\n101,224600222222\n")

    # Seed an 'interrupted' campaign row as a prior boot would have left it.
    old_id = "loop-deadbeef0001"
    loop_engine._persist_campaign({
        "id": old_id, "name": "Guinea-22460", "status": "interrupted",
        "node_id": None, "local_ip": "", "dest_host": "1.2.3.4",
        "dest_port": 5060, "transport": "udp", "csv_path": str(csv),
        "rate": 2.0, "max_concurrent": 3, "duration_mode": "fixed",
        "duration_s": 1, "duration_max_s": 0, "match_key": "exact",
        "target_calls": 0, "target_minutes": 0,
        "created_at": "2026-01-01T00:00:00+00:00",
        "started_at": "2026-01-01T00:00:00+00:00", "stopped_at": None,
    })

    report = loop_engine.resume_interrupted()
    assert len(report["resumed"]) == 1
    new_id = report["resumed"][0]
    assert new_id != old_id

    # The old row is now terminal (won't be resumed again next boot).
    with db.engine.connect() as conn:
        old_status = conn.execute(
            text("SELECT status FROM loop_campaigns WHERE id = :id"), {"id": old_id}
        ).fetchone()[0]
        new_status = conn.execute(
            text("SELECT status, name FROM loop_campaigns WHERE id = :id"), {"id": new_id}
        ).fetchone()
    assert old_status == "stopped"
    assert new_status[0] == "running"
    assert new_status[1] == "Guinea-22460"   # name carried over so grouping holds

    # A second resume is a no-op — nothing left in a non-terminal state.
    assert loop_engine.resume_interrupted() == {"resumed": [], "skipped": []}


def test_resume_interrupted_skips_when_pool_missing(loop_engine, db):
    """A campaign whose pool CSV is gone and has no node can't be resumed — it's
    skipped (and still marked terminal), never crashing the resume of others."""
    from sqlalchemy import text
    old_id = "loop-deadbeef0002"
    loop_engine._persist_campaign({
        "id": old_id, "name": "Orphan", "status": "interrupted",
        "node_id": None, "local_ip": "", "dest_host": "1.2.3.4",
        "dest_port": 5060, "transport": "udp", "csv_path": "/tmp/gone_xyz.csv",
        "rate": 1.0, "max_concurrent": 1, "duration_mode": "fixed",
        "duration_s": 1, "duration_max_s": 0, "match_key": "exact",
        "target_calls": 0, "target_minutes": 0,
        "created_at": "2026-01-01T00:00:00+00:00",
        "started_at": "2026-01-01T00:00:00+00:00", "stopped_at": None,
    })
    report = loop_engine.resume_interrupted()
    assert old_id in report["skipped"]
    with db.engine.connect() as conn:
        status = conn.execute(
            text("SELECT status FROM loop_campaigns WHERE id = :id"), {"id": old_id}
        ).fetchone()[0]
    assert status == "stopped"


def test_local_ip_binds_uac_and_persists(loop_engine, db):
    """A per-loop source IP binds the UAC and is recorded on the campaign + row."""
    campaign = loop_engine.start_campaign(
        dest_host="1.2.3.4", rate=1.0, duration_s=1, local_ip="10.9.9.9",
    )
    cid = campaign["id"]
    assert campaign["local_ip"] == "10.9.9.9"
    inst = _wait_running(loop_engine.engine, f"uac-{cid}")
    assert inst.local_ip == "10.9.9.9"  # SIPp -i/-mi bind

    from sqlalchemy import text
    with db.engine.connect() as conn:
        row = conn.execute(
            text("SELECT local_ip FROM loop_campaigns WHERE id = :id"), {"id": cid}
        ).fetchone()
    assert row[0] == "10.9.9.9"


def test_one_loop_per_ip_is_enforced(loop_engine):
    """A second running loop on the SAME source IP is refused; a different IP is ok."""
    first = loop_engine.start_campaign(
        dest_host="1.2.3.4", rate=1.0, duration_s=30, local_ip="10.0.0.5")
    _wait_running(loop_engine.engine, f"uac-{first['id']}")

    with pytest.raises(IPBusy):
        loop_engine.start_campaign(
            dest_host="1.2.3.4", rate=1.0, duration_s=30, local_ip="10.0.0.5")

    # A different IP is allowed concurrently.
    second = loop_engine.start_campaign(
        dest_host="1.2.3.4", rate=1.0, duration_s=30, local_ip="10.0.0.6")
    assert second["status"] == "running"


def test_stop_campaign_kills_uac_and_marks_stopped(loop_engine, db):
    """Stopping a campaign kills its UAC and flips the row to 'stopped'."""
    campaign = loop_engine.start_campaign(
        dest_host="1.2.3.4", rate=2.0, duration_s=1,
    )
    cid = campaign["id"]
    inst = _wait_running(loop_engine.engine, f"uac-{cid}")
    assert inst.state == SIPpState.RUNNING

    result = loop_engine.stop_campaign(cid)
    assert result["status"] == "stopped"

    # Process is no longer RUNNING.
    inst = loop_engine.engine.get_instance(f"uac-{cid}")
    assert inst.state != SIPpState.RUNNING

    from sqlalchemy import text
    with db.engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, stopped_at FROM loop_campaigns WHERE id = :id"),
            {"id": cid},
        ).fetchone()
    assert row[0] == "stopped"
    assert row[1] is not None


def test_caps_enforced_refuses_over_limit_start(stub_sipp, db, monkeypatch):
    """An over-limit start is refused with CapExceeded (design §4.1)."""
    # Force the concurrent cap down to 1 via config override.
    config = stub_sipp.config
    monkeypatch.setattr(type(config), "loops_max_concurrent",
                        property(lambda self: 1))
    registry = ProcessRegistry(db=db)
    engine = SIPpEngine(config=config, registry=registry)
    le = LoopEngine(engine, db=db, config=config)
    try:
        first = le.start_campaign(dest_host="1.2.3.4", rate=1.0, duration_s=5)
        _wait_running(engine, f"uac-{first['id']}")
        # Second start would breach the cap of 1 -> refused.
        with pytest.raises(CapExceeded):
            le.start_campaign(dest_host="1.2.3.4", rate=1.0, duration_s=5)
    finally:
        le.stop_monitor()
        engine.stop_all()


def test_answer_status_reports_uas(loop_engine):
    """start_answer launches the persistent UAS; answer_status reports it."""
    ok = loop_engine.start_answer()
    assert ok is True
    inst = _wait_running(loop_engine.engine, UAS_INSTANCE_ID)
    assert inst is not None
    status = loop_engine.answer_status()
    assert status["running"] is True
    assert status["state"] == SIPpState.RUNNING.value
    assert status["max_answered"] == loop_engine.config.loops_max_answered


# ─── Call-path bug regressions (review-confirmed) ─────────────────────────────

def _csv_rows(path):
    with open(path, "r", encoding="utf-8") as fh:
        return [ln.rstrip("\n") for ln in fh if ln.strip()]


def test_fixed_duration_bakes_nonempty_field2_ms(loop_engine):
    """Fixed mode writes a real per-call hold into the -inf field2 (ms).

    The UAC scenario holds with <pause milliseconds="[field2]"/>; an empty
    field2 made every call hold ~0s. The hold must now be present and in ms.
    """
    csv = loop_engine._prepare_csv("", "fixed", 3, 0)  # 3 s fixed
    rows = _csv_rows(csv)
    # RANDOM so each call draws a random pair from the pool (was SEQUENTIAL).
    assert rows[0].upper() == "RANDOM"
    # First data row: a;b;hold_ms;  -> field2 is column index 2 == 3000 ms.
    cells = [c for c in rows[1].split(";")]
    assert cells[2] == "3000"


def test_fixed_duration_preserves_subsecond_via_ms(loop_engine):
    """Hold travels in ms end-to-end (no //1000 then *1000 truncation)."""
    # 1 s -> exactly 1000 ms, not collapsed to 0 by an integer round-trip.
    csv = loop_engine._prepare_csv("", "fixed", 1, 0)
    cells = _csv_rows(csv)[1].split(";")
    assert cells[2] == "1000"


def test_range_duration_field2_within_window(loop_engine):
    """Range mode bakes a per-row uniform hold (ms) inside [lo, hi]."""
    csv = loop_engine._prepare_csv("", "range", 2, 5)  # 2000..5000 ms
    for row in _csv_rows(csv)[1:]:
        hold = int(row.split(";")[2])
        assert 2000 <= hold <= 5000


def test_uas_command_has_i_mi_and_mp(loop_engine, monkeypatch):
    """The persistent UAS binds the SIP-facing IP (-i/-mi) + an RTP echo port (-mp)."""
    # Set a SIP-facing local IP so -i/-mi are emitted.
    monkeypatch.setattr(type(loop_engine.config), "sip_local_ip",
                        property(lambda self: "10.9.9.9"))
    inst = loop_engine._build_uas_instance()
    cmd = inst.build_command(loop_engine.config)
    assert "-i" in cmd and cmd[cmd.index("-i") + 1] == "10.9.9.9"
    assert "-mi" in cmd and cmd[cmd.index("-mi") + 1] == "10.9.9.9"
    assert "-mp" in cmd
    assert "-min_rtp_port" not in cmd and "-max_rtp_port" not in cmd


def test_uas_max_duration_guard_is_a_literal_not_a_key(loop_engine):
    """The answered-call max-duration guard is a LITERAL integer in loop_uas.xml's
    recv timeout — SIPp does not substitute a -key keyword inside a timeout
    attribute, so the old "[duration_max_s]000" form failed to load. The UAS
    therefore no longer passes -key duration_max_s; it just passes -rtp_echo."""
    import re
    inst = loop_engine._build_uas_instance()
    assert inst.extra_args == "-rtp_echo"
    assert "duration_max_s" not in inst.extra_args
    xml = open(inst.scenario_file, encoding="latin-1").read()
    m = re.search(r'request="BYE"\s+timeout="(\d+)"', xml)
    assert m and int(m.group(1)) > 0, "UAS recv BYE timeout must be a positive integer literal"


# ─── CallRecordParser wiring (design §4.2 — the records BLOCKER) ──────────────

def _count_call_records(db):
    from sqlalchemy import text
    with db.engine.connect() as conn:
        return conn.execute(
            text("SELECT COUNT(*) FROM call_records")
        ).fetchone()[0]


def test_start_campaign_registers_log_with_parser(loop_engine, db):
    """Starting a campaign registers the UAC's per-call log path with the wired
    parser AND the parser ingests the stub's emitted records into call_records.

    This is the end-to-end records path: without it call_records stays empty and
    every minutes/completion stat is permanently 0.
    """
    from gencall.core.call_records import CallRecordParser

    parser = CallRecordParser(db=db)
    loop_engine.parser = parser

    campaign = loop_engine.start_campaign(
        name="rec", dest_host="1.2.3.4", rate=20.0, max_concurrent=10,
        duration_s=1, target_calls=20,
    )
    cid = campaign["id"]

    # The campaign's UAC log path(s) are now tracked by the parser.
    inst = loop_engine.engine.get_instance(f"uac-{cid}")
    candidates = inst.log_file_candidates()
    assert candidates, "instance must expose a per-call log path"
    assert any(p in parser._files for p in candidates), (
        "start_campaign must register the UAC log with the parser"
    )

    # Let the finite stub run write its call log, then poll the parser.
    deadline = time.time() + 30
    while inst.state == SIPpState.RUNNING and time.time() < deadline:
        time.sleep(0.1)
    # A couple of polls to drain everything the stub wrote.
    for _ in range(3):
        parser.poll_once()
        time.sleep(0.05)

    assert _count_call_records(db) > 0, (
        "the wired parser must ingest the campaign's call records"
    )


def test_create_app_wires_parser_and_ingests_end_to_end(stub_sipp, tmp_path,
                                                         monkeypatch):
    """create_app() instantiates and wires a CallRecordParser, and a campaign
    started through the wired LoopEngine lands records in call_records.

    Proves the production wiring (not just isolated units): the parser is
    instantiated in create_app, attached to the LoopEngine, started, and given
    the trust whitelist. This is the BLOCKER the review flagged (parser never
    instantiated in main.py).
    """
    import textwrap

    from gencall.core.config import Config

    # A config pointing [sipp] at the stub and [database] at a temp sqlite file.
    db_path = tmp_path / "appwire.db"
    cfg_path = tmp_path / "appwire.cfg"
    cfg_path.write_text(
        textwrap.dedent(
            f"""\
            [sipp]
            command = {stub_sipp.launcher}
            stats_dir = {stub_sipp.stats_dir}
            open_file_limit = 256
            default_transport = udp

            [database]
            engine = sqlite
            sqlite_path = {db_path}

            [trust]
            whitelist = 10.0.0.0/24
            """
        ),
        encoding="utf-8",
    )

    Config.reset()
    monkeypatch.setenv("GENCALL_CONFIG", str(cfg_path))

    # NB: `import gencall.main` (not `from gencall import main`) — the package
    # __init__ sets __name__ = "GenCall", which breaks the `from gencall import
    # <submodule>` form but not the dotted-import form.
    import gencall.main as gc_main
    from gencall.api import loops as loops_api

    app, config = gc_main.create_app(str(cfg_path))
    try:
        le = loops_api.loop_engine
        parser = le.parser
        # The parser was instantiated, wired to the engine, and carries the
        # configured trust whitelist (proving config.trust_whitelist is passed).
        assert parser is not None, "create_app must wire a CallRecordParser"
        assert parser.trust_whitelist == ["10.0.0.0/24"]

        # Start a finite campaign through the wired engine.
        campaign = le.start_campaign(
            name="wired", dest_host="1.2.3.4", rate=20.0, max_concurrent=10,
            duration_s=1, target_calls=20,
        )
        cid = campaign["id"]
        inst = le.engine.get_instance(f"uac-{cid}")
        deadline = time.time() + 30
        while inst.state == SIPpState.RUNNING and time.time() < deadline:
            time.sleep(0.1)
        for _ in range(3):
            parser.poll_once()
            time.sleep(0.05)

        from sqlalchemy import text
        with le.db.engine.connect() as conn:
            n = conn.execute(
                text("SELECT COUNT(*) FROM call_records WHERE campaign_id = :c"),
                {"c": cid},
            ).fetchone()[0]
        assert n > 0, "records ingested through the create_app wiring"
    finally:
        try:
            le.stop_monitor()
            le.engine.stop_all()
            parser.stop()
        except Exception:
            pass
        Config.reset()


def test_node_group_start_stop_fans_out_over_http(stub_sipp, tmp_path, monkeypatch):
    """Create a group + two member nodes, start the group (a loop per node on its
    own IP), then stop the group (all member loops stop)."""
    import textwrap
    from fastapi.testclient import TestClient

    from gencall.core.config import Config
    from gencall.core.api_gateway import APIKeyManager

    db_path = tmp_path / "grp.db"
    cfg_path = tmp_path / "grp.cfg"
    cfg_path.write_text(textwrap.dedent(f"""\
        [sipp]
        command = {stub_sipp.launcher}
        stats_dir = {stub_sipp.stats_dir}
        open_file_limit = 256
        [database]
        engine = sqlite
        sqlite_path = {db_path}
        """), encoding="utf-8")

    Config.reset()
    monkeypatch.setenv("GENCALL_CONFIG", str(cfg_path))
    import gencall.main as gc_main
    from gencall.api import loops as loops_api

    app, _config = gc_main.create_app(str(cfg_path))
    le = loops_api.loop_engine
    raw_key, _ = APIKeyManager(db=le.db).create_key("grp")
    client = TestClient(app)
    client.headers.update({"X-API-Key": raw_key})
    try:
        # Group with a shared (public, SSRF-allowed) destination route.
        g = client.post("/api/node-groups", json={
            "name": "guinea-route", "dest_host": "1.2.3.4", "dest_port": 5060,
            "rate": 5.0, "duration_s": 1, "target_calls": 5,
        })
        assert g.status_code == 200, g.text
        gid = g.json()["group"]["id"]

        # Two member nodes, each with its own IP + pool.
        for ip in ("10.0.0.61", "10.0.0.62"):
            r = client.post("/api/servers", json={
                "name": f"node-{ip}", "ip": ip, "group_id": gid,
                "origin_zone": "Nigeria-Lagos", "dest_zone": "Guinea-Mobile (Orange)",
                "count": 10,
            })
            assert r.status_code == 200, r.text

        # Group list reflects the two members.
        groups = client.get("/api/node-groups").json()["groups"]
        grp = next(x for x in groups if x["id"] == gid)
        assert grp["node_count"] == 2

        # Member node ids (for the subset-start check).
        member_ids = [n["id"] for n in client.get("/api/servers").json()["servers"]
                      if n["group_id"] == gid]
        assert len(member_ids) == 2

        # Partial start: run ONLY the first node (we don't always run the whole group).
        one = client.post(f"/api/node-groups/{gid}/start",
                          json={"node_ids": [member_ids[0]]})
        assert one.status_code == 200, one.text
        assert one.json()["started"] == 1
        # Stop it again so the full-start below is clean.
        client.post(f"/api/node-groups/{gid}/stop")

        # Start the group → a loop fans out to BOTH nodes (no subset).
        started = client.post(f"/api/node-groups/{gid}/start")
        assert started.status_code == 200, started.text
        body = started.json()
        assert body["started"] == 2, body
        ips_running = {r["ip"] for r in body["results"] if r["ok"]}
        assert ips_running == {"10.0.0.61", "10.0.0.62"}

        # Both campaigns are live on their own IPs.
        for cid in [r["campaign_id"] for r in body["results"]]:
            _wait_running(le.engine, f"uac-{cid}")

        # Stop the group → both member loops stop.
        stopped = client.post(f"/api/node-groups/{gid}/stop")
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["stopped"] >= 1
    finally:
        try:
            le.stop_monitor()
            le.engine.stop_all()
            le.parser.stop()
        except Exception:
            pass
        Config.reset()


def test_loop_preset_save_run_and_history_over_http(stub_sipp, tmp_path, monkeypatch):
    """A saved loop preset (recipe only) runs on a chosen node AND fans out over a
    group, and every run lands in the loop history — the 'preconfigured loop, click
    run, see it in History' flow."""
    import textwrap
    from fastapi.testclient import TestClient

    from gencall.core.config import Config
    from gencall.core.api_gateway import APIKeyManager

    db_path = tmp_path / "preset.db"
    cfg_path = tmp_path / "preset.cfg"
    cfg_path.write_text(textwrap.dedent(f"""\
        [sipp]
        command = {stub_sipp.launcher}
        stats_dir = {stub_sipp.stats_dir}
        open_file_limit = 256
        [database]
        engine = sqlite
        sqlite_path = {db_path}
        """), encoding="utf-8")

    Config.reset()
    monkeypatch.setenv("GENCALL_CONFIG", str(cfg_path))
    import gencall.main as gc_main
    from gencall.api import loops as loops_api

    app, _config = gc_main.create_app(str(cfg_path))
    le = loops_api.loop_engine
    raw_key, _ = APIKeyManager(db=le.db).create_key("preset")
    client = TestClient(app)
    client.headers.update({"X-API-Key": raw_key})
    try:
        # Save a preset: the recipe (dest + ACD/rate/targets), NO source.
        p = client.post("/api/loop-presets", json={
            "name": "guinea-1m90", "dest_host": "1.2.3.4", "dest_port": 5060,
            "rate": 5.0, "duration_s": 1, "target_calls": 3,
        })
        assert p.status_code == 200, p.text
        pid = p.json()["preset"]["id"]
        assert client.get("/api/loop-presets").json()["presets"][0]["name"] == "guinea-1m90"

        # Running with neither a node nor a group is a 422.
        assert client.post(f"/api/loop-presets/{pid}/run", json={}).status_code == 422

        # A node with its own IP + generated pool.
        node = client.post("/api/servers", json={
            "name": "node-a", "ip": "10.0.0.71",
            "origin_zone": "Nigeria-Lagos", "dest_zone": "Guinea-Mobile (Orange)",
            "count": 10,
        })
        assert node.status_code == 200, node.text
        nid = node.json()["server"]["id"]

        # Run the preset on that node → one loop on its IP.
        run = client.post(f"/api/loop-presets/{pid}/run", json={"node_id": nid})
        assert run.status_code == 200, run.text
        assert run.json()["started"] == 1
        cid = run.json()["results"][0]["campaign_id"]
        _wait_running(le.engine, f"uac-{cid}")

        # The run is in the loop history, newest first, carrying the preset's dest.
        runs = client.get("/api/loops/history").json()["runs"]
        assert any(r["id"] == cid for r in runs)
        assert runs[0]["dest_host"] == "1.2.3.4"
        client.post(f"/api/loops/{cid}/stop")

        # Run the SAME preset on a group of two nodes → fans out to both IPs.
        gid = client.post("/api/node-groups",
                          json={"name": "grp-x", "dest_host": "1.2.3.4"}).json()["group"]["id"]
        for ip in ("10.0.0.72", "10.0.0.73"):
            client.post("/api/servers", json={
                "name": f"node-{ip}", "ip": ip, "group_id": gid,
                "origin_zone": "Nigeria-Lagos", "dest_zone": "Guinea-Mobile (Orange)",
                "count": 10,
            })
        grun = client.post(f"/api/loop-presets/{pid}/run", json={"group_id": gid})
        assert grun.status_code == 200, grun.text
        assert grun.json()["started"] == 2, grun.text
        for r in grun.json()["results"]:
            if r["ok"]:
                _wait_running(le.engine, f"uac-{r['campaign_id']}")
    finally:
        try:
            le.stop_monitor()
            le.engine.stop_all()
            le.parser.stop()
        except Exception:
            pass
        Config.reset()


def test_node_pool_pins_drop_code(stub_sipp, tmp_path, monkeypatch):
    """A node created with a pinned dest_code generates B-numbers from ONLY that
    code — so we never dial the 224720/224721 Orange breakouts the switch has no
    route for (every CDPN must start with the pinned 22462)."""
    import textwrap
    from fastapi.testclient import TestClient

    from gencall.core.config import Config
    from gencall.core.api_gateway import APIKeyManager

    db_path = tmp_path / "pin.db"
    cfg_path = tmp_path / "pin.cfg"
    cfg_path.write_text(textwrap.dedent(f"""\
        [sipp]
        command = {stub_sipp.launcher}
        stats_dir = {stub_sipp.stats_dir}
        open_file_limit = 256
        [database]
        engine = sqlite
        sqlite_path = {db_path}
        """), encoding="utf-8")

    Config.reset()
    monkeypatch.setenv("GENCALL_CONFIG", str(cfg_path))
    import gencall.main as gc_main
    from gencall.api import loops as loops_api

    app, _config = gc_main.create_app(str(cfg_path))
    le = loops_api.loop_engine
    raw_key, _ = APIKeyManager(db=le.db).create_key("pin")
    client = TestClient(app)
    client.headers.update({"X-API-Key": raw_key})
    try:
        r = client.post("/api/servers", json={
            "name": "pin-node", "ip": "10.0.0.91",
            "origin_zone": "Nigeria-Lagos",
            "dest_zone": "Guinea-Mobile (Orange)", "dest_code": "22462",
            "count": 40,
        })
        assert r.status_code == 200, r.text
        srv = r.json()["server"]
        assert srv["dest_code"] == "22462"
        assert srv["has_pool"]
        with open(srv["csv_path"], encoding="utf-8") as f:
            rows = [ln.strip() for ln in f if ln.strip()]
        assert rows, "pool file should have numbers"
        for ln in rows:
            _a, b = ln.split(";")
            assert b.startswith("22462"), f"unrouted code generated: {b}"
    finally:
        try:
            le.stop_monitor()
            le.engine.stop_all()
            le.parser.stop()
        except Exception:
            pass
        Config.reset()


def test_remote_node_pool_gen_proxies_to_worker(stub_sipp, tmp_path, monkeypatch):
    """A node with api_url is REMOTE: its pool generation proxies to that worker.
    Pointed at an unreachable worker, create returns 502 (the remote path was
    taken, not local generation). A local node (no api_url) still generates a
    pool here and reports remote=False."""
    import textwrap
    from fastapi.testclient import TestClient

    from gencall.core.config import Config
    from gencall.core.api_gateway import APIKeyManager

    db_path = tmp_path / "remote.db"
    cfg_path = tmp_path / "remote.cfg"
    cfg_path.write_text(textwrap.dedent(f"""\
        [sipp]
        command = {stub_sipp.launcher}
        stats_dir = {stub_sipp.stats_dir}
        open_file_limit = 256
        [database]
        engine = sqlite
        sqlite_path = {db_path}
        """), encoding="utf-8")

    Config.reset()
    monkeypatch.setenv("GENCALL_CONFIG", str(cfg_path))
    import gencall.main as gc_main
    from gencall.api import loops as loops_api

    app, _config = gc_main.create_app(str(cfg_path))
    le = loops_api.loop_engine
    raw_key, _ = APIKeyManager(db=le.db).create_key("remote")
    client = TestClient(app)
    client.headers.update({"X-API-Key": raw_key})
    try:
        # Remote node pointed at an unreachable worker -> pool-gen proxy fails (502).
        r = client.post("/api/servers", json={
            "name": "remote-x", "ip": "10.0.0.80",
            "api_url": "http://127.0.0.1:9", "api_key": "gc_x",
            "origin_zone": "Nigeria-Lagos", "dest_zone": "Guinea-Mobile (Orange)",
            "dest_code": "22462", "count": 5,
        })
        assert r.status_code == 502, r.text  # remote path taken, not local

        # check-worker probe of an unreachable worker reports offline (no raise).
        chk = client.post("/api/servers/check-worker",
                          json={"api_url": "http://127.0.0.1:9", "api_key": "x"})
        assert chk.status_code == 200 and chk.json()["online"] is False

        # Local node still generates its pool here.
        r2 = client.post("/api/servers", json={
            "name": "local-x", "ip": "10.0.0.81",
            "origin_zone": "Nigeria-Lagos", "dest_zone": "Guinea-Mobile (Orange)",
            "dest_code": "22462", "count": 5,
        })
        assert r2.status_code == 200, r2.text
        srv = r2.json()["server"]
        assert srv["remote"] is False and srv["has_pool"], srv
    finally:
        try:
            le.stop_monitor()
            le.engine.stop_all()
            le.parser.stop()
        except Exception:
            pass
        Config.reset()


def test_fleet_resources_local_and_remote(stub_sipp, tmp_path, monkeypatch):
    """The Fleet page data: /api/resources reports this box's CPU/RAM, and
    /api/fleet/resources rolls up per node — a local node reports online with a
    hostname, a node pointed at an unreachable worker reports online=False with
    an error (it doesn't raise)."""
    import textwrap
    from fastapi.testclient import TestClient

    from gencall.core.config import Config
    from gencall.core.api_gateway import APIKeyManager

    db_path = tmp_path / "res.db"
    cfg_path = tmp_path / "res.cfg"
    cfg_path.write_text(textwrap.dedent(f"""\
        [sipp]
        command = {stub_sipp.launcher}
        stats_dir = {stub_sipp.stats_dir}
        open_file_limit = 256
        [database]
        engine = sqlite
        sqlite_path = {db_path}
        """), encoding="utf-8")

    Config.reset()
    monkeypatch.setenv("GENCALL_CONFIG", str(cfg_path))
    import gencall.main as gc_main
    from gencall.api import loops as loops_api

    app, _config = gc_main.create_app(str(cfg_path))
    le = loops_api.loop_engine
    raw_key, _ = APIKeyManager(db=le.db).create_key("res")
    client = TestClient(app)
    client.headers.update({"X-API-Key": raw_key})
    try:
        # This box's own resources. cores is always derivable; the CPU/RAM fields
        # exist even if a value is None (no psutil + non-Linux dev box).
        rself = client.get("/api/resources")
        assert rself.status_code == 200, rself.text
        body = rself.json()
        for k in ("hostname", "cpu_percent", "cores",
                  "mem_total_mb", "mem_used_mb", "mem_percent"):
            assert k in body, body

        # A local node (this box) and a remote node (unreachable worker).
        client.post("/api/servers", json={"name": "loc", "ip": "10.0.0.91"})
        client.post("/api/servers", json={
            "name": "rem", "ip": "10.0.0.92",
            "api_url": "http://127.0.0.1:9", "api_key": "gc_x",
        })

        fleet = client.get("/api/fleet/resources")
        assert fleet.status_code == 200, fleet.text
        nodes = {n["ip"]: n for n in fleet.json()["nodes"]}
        assert "10.0.0.91" in nodes and "10.0.0.92" in nodes, nodes

        local = nodes["10.0.0.91"]
        assert local["remote"] is False and local["box"] == "local"
        assert local["online"] is True
        assert local["hostname"]  # came from _box_resources()

        remote = nodes["10.0.0.92"]
        assert remote["remote"] is True
        assert remote["online"] is False and remote["error"]  # probe failed, no raise
    finally:
        try:
            le.stop_monitor()
            le.engine.stop_all()
            le.parser.stop()
        except Exception:
            pass
        Config.reset()


def test_uac_scenario_rtp_rendering(tmp_path):
    """RTP toggle: signaling-only returns the shipped template unchanged; RTP on
    renders a per-campaign scenario that injects a single rtp_stream exec (after
    the ACK, well-formed, failure branches intact). Loop count is -1 (looped)
    vs 1 (once). A missing/empty audio file falls back to signaling-only so a bad
    media config never blocks a loop."""
    import xml.etree.ElementTree as ET
    from gencall.core.loop_engine import LoopEngine, UAC_TEMPLATE

    # rtp_stream needs a raw audio file; the engine only checks it exists (SIPp
    # reads the bytes at runtime), so any file works for the render test.
    audio = tmp_path / "g711a.raw"
    audio.write_bytes(b"\xd5" * 160)

    class _CfgOn:
        loops_rtp_audio = str(audio)
    le = LoopEngine(sipp_engine=None, db=None, config=_CfgOn())

    # Off → the template as-is (no <nop>/<exec> added; bare -d <pause> intact).
    assert le._uac_scenario(False) == UAC_TEMPLATE

    # On, play once → one rtp_stream exec with loop count 1, pointing at the file.
    once = le._uac_scenario(True, rtp_loop=False)
    assert once != UAC_TEMPLATE
    root1 = ET.parse(once).getroot()                      # well-formed
    streams = [el.get("rtp_stream") for el in root1.iter("exec") if el.get("rtp_stream")]
    assert len(streams) == 1, "exactly one rtp_stream exec"
    assert "g711a.raw" in streams[0] and streams[0].endswith(",1,8,PCMA/8000")
    # The bare -d hold pause survives (rtp_stream is non-blocking).
    assert any(p.get("milliseconds") is None for p in root1.iter("pause"))
    # Failure branches survive the render (real ASR/NER still captured).
    logs = " ".join(el.get("message", "") for el in root1.iter("log"))
    assert "event=fail" in logs

    # On, looped → loop count -1 (stream the whole call).
    loop = le._uac_scenario(True, rtp_loop=True)
    root2 = ET.parse(loop).getroot()
    s2 = next(el.get("rtp_stream") for el in root2.iter("exec") if el.get("rtp_stream"))
    assert s2.endswith(",-1,8,PCMA/8000"), "looped RTP must use loop count -1"

    # Missing / empty audio → graceful fallback to signaling-only (no crash).
    class _CfgMissing:
        loops_rtp_audio = "/no/such/file.raw"
    assert LoopEngine(sipp_engine=None, db=None,
                      config=_CfgMissing())._uac_scenario(True) == UAC_TEMPLATE

    class _CfgEmpty:
        loops_rtp_audio = ""
    assert LoopEngine(sipp_engine=None, db=None,
                      config=_CfgEmpty())._uac_scenario(True) == UAC_TEMPLATE


def test_failure_ack_reuses_invite_via_branch():
    """The non-2xx (label 40) ACK must echo the received Via ([last_Via:]) so its
    branch matches the INVITE transaction (RFC 3261 §17.1.1.3). A fresh
    branch=[branch] there does NOT match, so the switch retransmits the 4xx
    (the Chad 404-flood). The 2xx ACK is a new transaction and keeps [branch]."""
    from gencall.core.loop_engine import UAC_TEMPLATE

    with open(UAC_TEMPLATE) as fh:
        template = fh.read()
    # Isolate the failure-ACK block (from `<label id="40"` to its </send>).
    after40 = template.split('<label id="40"', 1)[1]
    fail_ack = after40.split("</send>", 1)[0]
    assert "CSeq: 102 ACK" in fail_ack, "label-40 block must be the failure ACK"
    assert "[last_Via:]" in fail_ack, "failure ACK must echo the INVITE's Via"
    assert "branch=[branch]" not in fail_ack, (
        "failure ACK must NOT mint a new branch — it would not match the "
        "INVITE transaction and the switch would keep retransmitting the 4xx"
    )
    # The INVITE itself still mints its own branch.
    invite = template.split("INVITE sip:", 1)[1].split("</send>", 1)[0]
    assert "branch=[branch]" in invite


def test_node_to_loop_end_to_end_over_http(stub_sipp, tmp_path, monkeypatch):
    """FULL new flow through the REAL app + HTTP: add a node (pool generated from
    the sample deck) -> start a loop by node_id -> it runs on the node's IP and
    its pool, and call_records land. This is the user-facing path, end to end."""
    import textwrap
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from gencall.core.config import Config
    from gencall.core.api_gateway import APIKeyManager

    db_path = tmp_path / "e2e.db"
    cfg_path = tmp_path / "e2e.cfg"
    cfg_path.write_text(textwrap.dedent(f"""\
        [sipp]
        command = {stub_sipp.launcher}
        stats_dir = {stub_sipp.stats_dir}
        open_file_limit = 256
        [database]
        engine = sqlite
        sqlite_path = {db_path}
        [trust]
        whitelist = 10.0.0.0/24
        """), encoding="utf-8")

    Config.reset()
    monkeypatch.setenv("GENCALL_CONFIG", str(cfg_path))
    import gencall.main as gc_main
    from gencall.api import loops as loops_api

    app, _config = gc_main.create_app(str(cfg_path))
    le = loops_api.loop_engine
    raw_key, _ = APIKeyManager(db=le.db).create_key("e2e")
    client = TestClient(app)
    client.headers.update({"X-API-Key": raw_key})
    try:
        # 1) Add a node — its A/B pool is generated server-side from the deck.
        made = client.post("/api/servers", json={
            "name": "vd-e2e", "ip": "10.0.0.50",
            "origin_zone": "Nigeria-Lagos", "dest_zone": "Guinea-Mobile (Orange)",
            "count": 25,
        })
        assert made.status_code == 200, made.text
        node = made.json()["server"]
        assert node["has_pool"] and node["pool_count"] == 25

        # 2) Start a loop by node_id — IP + pool come from the node.
        started = client.post("/api/loops", json={
            "node_id": node["id"], "dest_host": "1.2.3.4",
            "rate": 20.0, "max_concurrent": 10, "duration_s": 1, "target_calls": 20,
        })
        assert started.status_code == 200, started.text
        camp = started.json()["campaign"]
        cid = camp["id"]
        assert camp["local_ip"] == "10.0.0.50" and camp["node_id"] == node["id"]

        # 3) Let the finite UAC run; records ingest through the wired parser.
        inst = le.engine.get_instance(f"uac-{cid}")
        deadline = time.time() + 30
        while inst.state == SIPpState.RUNNING and time.time() < deadline:
            time.sleep(0.1)
        for _ in range(3):
            le.parser.poll_once()
            time.sleep(0.05)

        with le.db.engine.connect() as conn:
            n = conn.execute(
                text("SELECT COUNT(*) FROM call_records WHERE campaign_id = :c"),
                {"c": cid},
            ).fetchone()[0]
        assert n > 0, "records ingested for the node-driven loop"

        # 4) The node is busy → can't be deleted until the loop stops.
        assert client.delete(f"/api/servers/{node['id']}").status_code in (200, 409)
    finally:
        try:
            le.stop_monitor()
            le.engine.stop_all()
            le.parser.stop()
        except Exception:
            pass
        Config.reset()


# ─── API router tests (FastAPI TestClient) ────────────────────────────────────

@pytest.fixture
def api_client(loop_engine, db, monkeypatch):
    """A TestClient over the worker app with the loops router mounted.

    Auth is now fail-CLOSED: a missing gateway returns 503, so the fixture wires
    a REAL gateway (key store backed by the test sqlite db) and mints a known
    key. Every request carries that key via the client's default headers. The
    router's module-level loop_engine is pointed at ours.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from gencall.api import routes
    from gencall.api import loops as loops_api
    from gencall.core.api_gateway import APIGateway, APIKeyManager

    gateway = APIGateway()
    gateway.keys = APIKeyManager(db=db)
    raw_key, _ = gateway.keys.create_key("loop-test")
    monkeypatch.setattr(routes, "gateway", gateway, raising=False)
    monkeypatch.setattr(loops_api, "loop_engine", loop_engine, raising=False)

    app = FastAPI()
    app.include_router(loops_api.router)
    client = TestClient(app)
    client.headers.update({"X-API-Key": raw_key})
    return client


def test_sale_zones_endpoint_returns_country_tree(api_client):
    """GET /api/sale-zones returns a country -> [zones] tree for the pickers."""
    resp = api_client.get("/api/sale-zones")
    assert resp.status_code == 200, resp.text
    countries = {c["name"]: c["zones"] for c in resp.json()["countries"]}
    assert "Nigeria" in countries
    assert "Nigeria-Lagos" in countries["Nigeria"]
    assert "Guinea-Mobile (Orange)" in countries["Guinea"]


def test_generate_numbers_endpoint_writes_pool(api_client):
    """POST /api/loops/numbers generates a pool and returns its path + preview."""
    resp = api_client.post("/api/loops/numbers", json={
        "origin_zone": "Nigeria-Lagos",
        "dest_zone": "Guinea-Mobile (Orange)",
        "count": 25, "length": 11, "seed": 1,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 25
    assert body["csv_path"].endswith(".csv")
    # preview rows are bare A;B (no trailing ';'), A under the origin zone code.
    a, b = body["preview"][0].split(";")
    assert a.startswith("2341") and b.isdigit()
    # the written file is dialable straight into a campaign
    import os
    with open(body["csv_path"], encoding="utf-8") as fh:
        first = fh.readline().strip()
    assert first.count(";") == 1
    os.remove(body["csv_path"])


def test_generate_numbers_unknown_zone_is_422(api_client):
    resp = api_client.post("/api/loops/numbers", json={
        "origin_zone": "Atlantis", "dest_zone": "Guinea-Mobile (Orange)", "count": 5,
    })
    assert resp.status_code == 422, resp.text


def test_servers_crud_roundtrip(loop_engine, db, monkeypatch):
    """Add / list / delete a server through the worker /api/servers routes."""
    from fastapi.testclient import TestClient

    from gencall.api import routes
    from gencall.core.api_gateway import APIGateway, APIKeyManager

    gateway = APIGateway()
    gateway.keys = APIKeyManager(db=db)
    raw_key, _ = gateway.keys.create_key("srv-test")
    monkeypatch.setattr(routes, "gateway", gateway, raising=False)
    monkeypatch.setattr(routes, "db", db, raising=False)

    client = TestClient(routes.app)
    client.headers.update({"X-API-Key": raw_key})

    created = client.post("/api/servers", json={"name": "vd1", "ip": "10.0.0.7"})
    assert created.status_code == 200, created.text
    sid = created.json()["server"]["id"]

    listed = client.get("/api/servers").json()["servers"]
    assert any(s["ip"] == "10.0.0.7" and s["name"] == "vd1" for s in listed)

    assert client.delete(f"/api/servers/{sid}").status_code == 200
    assert all(s["id"] != sid for s in client.get("/api/servers").json()["servers"])


def test_create_node_with_pool_generates_numbers(loop_engine, db, monkeypatch):
    """Creating a node with origin/drop zones generates its A/B pool file."""
    import os

    from fastapi.testclient import TestClient
    from gencall.api import routes
    from gencall.core.api_gateway import APIGateway, APIKeyManager

    gateway = APIGateway()
    gateway.keys = APIKeyManager(db=db)
    raw_key, _ = gateway.keys.create_key("pool-test")
    monkeypatch.setattr(routes, "gateway", gateway, raising=False)
    monkeypatch.setattr(routes, "db", db, raising=False)
    client = TestClient(routes.app)
    client.headers.update({"X-API-Key": raw_key})

    resp = client.post("/api/servers", json={
        "name": "vd-ng", "ip": "10.0.0.9",
        "origin_zone": "Nigeria-Lagos", "dest_zone": "Guinea-Mobile (Orange)",
        "count": 20, "length": 11,
    })
    assert resp.status_code == 200, resp.text
    node = resp.json()["server"]
    assert node["has_pool"] and node["pool_count"] == 20
    assert os.path.isfile(node["csv_path"])
    with open(node["csv_path"], encoding="utf-8") as fh:
        a, b = fh.readline().strip().split(";")
    assert a.startswith("2341")
    os.remove(node["csv_path"])


def test_start_loop_by_node_id_uses_node_ip_and_pool(api_client, loop_engine, db, tmp_path):
    """A loop started with node_id binds the node's IP and dials its pool."""
    pool = tmp_path / "pool.csv"
    pool.write_text("2341000001;2246200001\n2341000002;2246200002\n")
    from gencall.db.models import Server
    session = db.get_session()
    try:
        s = Server(name="vd-node", ip="10.7.7.7", csv_path=str(pool),
                   origin_zone="Nigeria-Lagos", dest_zone="Guinea-Mobile (Orange)",
                   pool_count=2)
        session.add(s)
        session.commit()
        nid = s.id
    finally:
        session.close()

    resp = api_client.post("/api/loops", json={
        "node_id": nid, "dest_host": "1.2.3.4", "rate": 1.0, "duration_s": 1,
    })
    assert resp.status_code == 200, resp.text
    camp = resp.json()["campaign"]
    assert camp["local_ip"] == "10.7.7.7"
    assert camp["node_id"] == nid  # the node FK is persisted on the campaign

    from sqlalchemy import text
    with db.engine.connect() as conn:
        row = conn.execute(
            text("SELECT node_id, local_ip FROM loop_campaigns WHERE id = :id"),
            {"id": camp["id"]},
        ).fetchone()
    assert str(row[0]) == str(nid) and row[1] == "10.7.7.7"


def _routes_client(db, monkeypatch, label):
    """A TestClient over the worker app (routes.app) with auth + db wired."""
    from fastapi.testclient import TestClient
    from gencall.api import routes
    from gencall.core.api_gateway import APIGateway, APIKeyManager

    gateway = APIGateway()
    gateway.keys = APIKeyManager(db=db)
    raw_key, _ = gateway.keys.create_key(label)
    monkeypatch.setattr(routes, "gateway", gateway, raising=False)
    monkeypatch.setattr(routes, "db", db, raising=False)
    client = TestClient(routes.app)
    client.headers.update({"X-API-Key": raw_key})
    return client


def test_cannot_delete_or_regen_node_with_running_loop(loop_engine, db, monkeypatch):
    """Delete/regenerate is refused (409) while a loop runs on the node's IP."""
    camp = loop_engine.start_campaign(
        dest_host="1.2.3.4", rate=1.0, duration_s=30, local_ip="10.5.5.5")
    _wait_running(loop_engine.engine, f"uac-{camp['id']}")

    client = _routes_client(db, monkeypatch, "busy-test")
    created = client.post("/api/servers", json={"name": "n5", "ip": "10.5.5.5"})
    sid = created.json()["server"]["id"]

    assert client.delete(f"/api/servers/{sid}").status_code == 409
    assert client.post(f"/api/servers/{sid}/generate", json={
        "origin_zone": "Nigeria-Lagos", "dest_zone": "Guinea-Mobile (Orange)",
        "count": 10,
    }).status_code == 409


def test_node_pool_count_is_bounded(db, monkeypatch):
    """An over-large pool request is rejected (422), not generated."""
    client = _routes_client(db, monkeypatch, "bound-test")
    resp = client.post("/api/servers", json={
        "name": "big", "ip": "10.6.6.6",
        "origin_zone": "Nigeria-Lagos", "dest_zone": "Guinea-Mobile (Orange)",
        "count": 9_999_999,
    })
    assert resp.status_code == 422, resp.text


def test_ensure_added_columns_idempotent_and_upgrades(tmp_path):
    """create_tables() is idempotent, and a legacy `servers` table missing the
    pool columns gets them added (the deployed-box upgrade path)."""
    from sqlalchemy import text
    from gencall.db.models import Database

    url = f"sqlite:///{tmp_path / 'srv.db'}"
    # Simulate a legacy box: a `servers` table WITHOUT the new pool columns.
    legacy = Database.__new__(Database)
    from sqlalchemy import create_engine
    legacy.engine = create_engine(url)
    with legacy.engine.begin() as conn:
        conn.execute(text("CREATE TABLE servers (id INTEGER PRIMARY KEY, "
                          "name VARCHAR(255), ip VARCHAR(45))"))
    # Now bring up the real Database: create_tables() must add missing columns.
    db = Database(url)
    db.create_tables()           # second call must also be a no-op (idempotent)
    db.create_tables()
    with db.engine.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(servers)"))}
    assert {"origin_zone", "dest_zone", "pool_count", "pool_length", "csv_path"} <= cols


def test_start_loop_by_node_without_pool_is_422(api_client, db):
    """A node with no generated pool can't launch a loop (422, not a silent run)."""
    from gencall.db.models import Server
    session = db.get_session()
    try:
        s = Server(name="vd-empty", ip="10.7.7.8")
        session.add(s)
        session.commit()
        nid = s.id
    finally:
        session.close()
    resp = api_client.post("/api/loops", json={"node_id": nid, "dest_host": "1.2.3.4"})
    assert resp.status_code == 422, resp.text


def test_loop_endpoint_fails_closed_503_when_no_gateway(loop_engine, monkeypatch):
    """A loop endpoint must FAIL CLOSED (503), not open (200), when no gateway.

    Regression for the auth fail-OPEN bug: require_api_key previously returned
    None (allow) when gateway was None — opening every loop/fleet endpoint the
    moment the DB went away. With the key store absent, a state-changing /api/loops
    request must be refused with 503, never served.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from gencall.api import routes
    from gencall.api import loops as loops_api

    monkeypatch.setattr(routes, "gateway", None, raising=False)
    monkeypatch.setattr(loops_api, "loop_engine", loop_engine, raising=False)

    app = FastAPI()
    app.include_router(loops_api.router)
    client = TestClient(app)

    # List (read) loop endpoint: closed.
    assert client.get("/api/loops").status_code == 503
    # State-changing start: closed (never reaches the engine).
    resp = client.post("/api/loops", json={"dest_host": "9.9.9.9", "duration_s": 1})
    assert resp.status_code == 503, resp.text


def test_api_start_stop_and_list(api_client, loop_engine):
    """POST /api/loops starts; GET lists it; POST stop -> 'stopped'."""
    resp = api_client.post(
        "/api/loops",
        json={"name": "apic", "dest_host": "9.9.9.9", "rate": 2.0,
              "duration_s": 1},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "started"
    cid = body["campaign"]["id"]
    _wait_running(loop_engine.engine, f"uac-{cid}")

    listed = api_client.get("/api/loops")
    assert listed.status_code == 200
    assert cid in [c["id"] for c in listed.json()["campaigns"]]

    stopped = api_client.post(f"/api/loops/{cid}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["campaign"]["status"] == "stopped"


def test_api_caps_returns_409(api_client, loop_engine, monkeypatch):
    """An over-limit POST /api/loops is refused with HTTP 409."""
    monkeypatch.setattr(type(loop_engine.config), "loops_max_concurrent",
                        property(lambda self: 1))
    first = api_client.post(
        "/api/loops", json={"dest_host": "9.9.9.9", "rate": 1.0, "duration_s": 5},
    )
    assert first.status_code == 200, first.text
    _wait_running(loop_engine.engine, f"uac-{first.json()['campaign']['id']}")
    second = api_client.post(
        "/api/loops", json={"dest_host": "9.9.9.9", "rate": 1.0, "duration_s": 5},
    )
    assert second.status_code == 409, second.text


def test_api_answer_status(api_client, loop_engine):
    """GET /api/answer/status reports the persistent UAS once started."""
    loop_engine.start_answer()
    _wait_running(loop_engine.engine, UAS_INSTANCE_ID)
    resp = api_client.get("/api/answer/status")
    assert resp.status_code == 200, resp.text
    assert resp.json()["running"] is True


# ─── Input-validation hardening (review-confirmed security bugs) ──────────────

def test_api_rejects_out_of_range_rate(api_client):
    """rate <= 0 and rate above the config cap are both refused with 422."""
    # Zero/negative rate: structural 422 (pydantic gt=0).
    r = api_client.post("/api/loops", json={"dest_host": "9.9.9.9", "rate": 0})
    assert r.status_code == 422, r.text
    r = api_client.post("/api/loops", json={"dest_host": "9.9.9.9", "rate": -1})
    assert r.status_code == 422, r.text
    # Above the per-campaign cap (default 500 cps): config 422.
    r = api_client.post("/api/loops", json={"dest_host": "9.9.9.9", "rate": 100000})
    assert r.status_code == 422, r.text


def test_api_rejects_out_of_range_concurrency(api_client):
    """max_concurrent <= 0 and above the channel cap are both refused with 422."""
    r = api_client.post(
        "/api/loops", json={"dest_host": "9.9.9.9", "max_concurrent": 0})
    assert r.status_code == 422, r.text
    r = api_client.post(
        "/api/loops", json={"dest_host": "9.9.9.9", "max_concurrent": -5})
    assert r.status_code == 422, r.text
    # Above the per-campaign channel cap (default 1000).
    r = api_client.post(
        "/api/loops", json={"dest_host": "9.9.9.9", "max_concurrent": 9999})
    assert r.status_code == 422, r.text


def test_api_rejects_negative_durations_and_targets(api_client):
    """Negative durations / targets are refused with 422 (no negatives)."""
    for field in ("duration_s", "duration_max_s", "target_calls", "target_minutes"):
        r = api_client.post(
            "/api/loops", json={"dest_host": "9.9.9.9", field: -1})
        assert r.status_code == 422, f"{field}: {r.text}"


def test_api_rejects_out_of_range_port(api_client):
    """dest_port outside 1-65535 is refused with 422."""
    r = api_client.post("/api/loops", json={"dest_host": "9.9.9.9", "dest_port": 0})
    assert r.status_code == 422, r.text
    r = api_client.post(
        "/api/loops", json={"dest_host": "9.9.9.9", "dest_port": 70000})
    assert r.status_code == 422, r.text


def test_api_rejects_unknown_transport(api_client):
    """An unknown transport is a 422, never a silent downgrade to UDP."""
    r = api_client.post(
        "/api/loops", json={"dest_host": "9.9.9.9", "transport": "sctp"})
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("bad_host", [
    "127.0.0.1",       # loopback
    "10.20.8.40",      # private (RFC1918)
    "192.168.1.10",    # private
    "172.16.0.5",      # private
    "0.0.0.0",         # unspecified
    "224.0.0.1",       # multicast
    "169.254.1.1",     # link-local
])
def test_api_rejects_private_loopback_dest_host(api_client, bad_host):
    """A private/loopback/multicast/0.0.0.0 dest_host is refused with 422 (SSRF)."""
    r = api_client.post("/api/loops", json={"dest_host": bad_host, "duration_s": 1})
    assert r.status_code == 422, f"{bad_host}: {r.text}"


def test_api_allows_public_dest_host(api_client, loop_engine):
    """A public dest_host is accepted (the block is targeted, not blanket)."""
    r = api_client.post(
        "/api/loops", json={"dest_host": "9.9.9.9", "duration_s": 1})
    assert r.status_code == 200, r.text
    _wait_running(loop_engine.engine, f"uac-{r.json()['campaign']['id']}")


def test_dest_allowlist_permits_blocked_range(api_client, loop_engine, monkeypatch):
    """An explicit allow-list entry lets an otherwise-blocked private IP through."""
    monkeypatch.setattr(type(loop_engine.config), "loops_dest_allowlist",
                        property(lambda self: ["10.20.8.0/24"]))
    r = api_client.post(
        "/api/loops", json={"dest_host": "10.20.8.40", "duration_s": 1})
    assert r.status_code == 200, r.text
    _wait_running(loop_engine.engine, f"uac-{r.json()['campaign']['id']}")


def test_engine_rejects_unbounded_inputs_directly(loop_engine):
    """A direct engine caller (controller dispatch) is also bounded (OOM guard).

    The API model can't protect a non-HTTP caller, so start_campaign itself
    refuses non-positive rate/concurrency and over-cap values.
    """
    with pytest.raises(CapExceeded):
        loop_engine.start_campaign(dest_host="9.9.9.9", rate=0, duration_s=1)
    with pytest.raises(CapExceeded):
        loop_engine.start_campaign(dest_host="9.9.9.9", max_concurrent=0, duration_s=1)
    with pytest.raises(CapExceeded):
        loop_engine.start_campaign(
            dest_host="9.9.9.9", max_concurrent=99999, duration_s=1)


# ─── dest_host validation unit tests ──────────────────────────────────────────

def test_validate_dest_host_unit():
    from gencall.api.loop_validation import DestHostError, validate_dest_host

    # Public address passes.
    assert validate_dest_host("9.9.9.9") == "9.9.9.9"
    # Blocked ranges raise.
    for bad in ("127.0.0.1", "10.0.0.1", "192.168.0.1", "0.0.0.0", "224.0.0.1"):
        with pytest.raises(DestHostError):
            validate_dest_host(bad)
    # Allow-list bypasses the block for the exact CIDR.
    assert validate_dest_host("10.0.0.5", ["10.0.0.0/8"]) == "10.0.0.5"


def test_validate_transport_unit():
    from gencall.api.loop_validation import validate_transport

    assert validate_transport("UDP") == "udp"
    assert validate_transport("tls") == "tls"
    with pytest.raises(ValueError):
        validate_transport("sctp")


# ─── #5 adaptive-pool relaunch carries the -m call count forward ──────────────


def test_adaptive_restart_carries_forward_call_count(loop_engine, tmp_path):
    """The adaptive-pool UAC restart must carry the attempted-call count across
    relaunches (max_calls = target - already_placed), not reset SIPp's -m to the
    full target each rebuild — which made a call-targeted campaign overshoot."""
    eng = loop_engine
    c = eng.start_campaign(name="cf", dest_host="1.2.3.4", rate=5.0,
                           max_concurrent=10, duration_s=1, target_calls=100_000)
    cid = c["id"]
    # Simulate prior UAC generations having already placed 5000 calls.
    eng._campaigns[cid]["calls_placed"] = 5000
    pool = tmp_path / "pool.csv"
    pool.write_text("10001;20001\n10002;20002\n", encoding="utf-8")

    assert eng._restart_uac_with_csv(cid, str(pool)) is True
    placed = eng._campaigns[cid]["calls_placed"]
    assert placed >= 5000                          # prior count retained
    new = eng.engine.get_instance(f"uac-{cid}")
    assert new is not None
    assert new.max_calls == 100_000 - placed       # -m = remaining, not the full target
    eng.stop_campaign(cid)


def test_adaptive_restart_completes_when_target_reached(loop_engine, tmp_path):
    """When the cumulative target is already met, the restart finalizes the
    campaign instead of relaunching (max_calls=0 would mean UNLIMITED)."""
    eng = loop_engine
    c = eng.start_campaign(name="done", dest_host="1.2.3.4", rate=5.0,
                           max_concurrent=10, duration_s=1, target_calls=100)
    cid = c["id"]
    eng._campaigns[cid]["calls_placed"] = 100      # target already reached
    pool = tmp_path / "pool.csv"
    pool.write_text("10001;20001\n", encoding="utf-8")

    assert eng._restart_uac_with_csv(cid, str(pool)) is True
    assert eng._campaigns[cid]["status"] == "completed"
    assert eng.engine.get_instance(f"uac-{cid}") is None   # not relaunched
