"""
LoopEngine + loops API tests against the stub `sipp` (design §4.1 / §4.4 / §6).

Runs WITHOUT real SIPp/Docker/Linux: the conftest ``stub_sipp`` fixture points
``config.sipp_command`` at the cross-platform fake sipp (tests/stubs/fake_sipp.py)
and resets the Config singleton. We build a real SIPpEngine + ProcessRegistry +
sqlite Database + LoopEngine and exercise:

  * start a campaign     -> UAC spawned (PID registered) + a 'running' DB row;
  * stop a campaign      -> UAC process gone, status 'stopped';
  * caps enforced        -> an over-limit start is refused (CapExceeded / 409);
  * CSV export           -> records.csv returns the call_records rows;
  * answer status        -> the persistent UAS reports running.

Both the engine surface and the FastAPI router (via TestClient) are covered.
"""

import time

import pytest

from gencall.core.config import Config
from gencall.core.loop_engine import CapExceeded, LoopEngine, UAS_INSTANCE_ID
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


def test_records_csv_export_returns_rows(loop_engine, db):
    """records.csv export returns the campaign's call_records as CSV rows."""
    campaign = loop_engine.start_campaign(dest_host="1.2.3.4", duration_s=1)
    cid = campaign["id"]

    # Seed two call_records for this campaign directly (the tail-parser is
    # covered elsewhere; here we assert the export query/format).
    from sqlalchemy import text
    with db.engine.begin() as conn:
        for i in range(2):
            conn.execute(
                text(
                    "INSERT INTO call_records "
                    "(campaign_id, direction, call_uuid, a_number, b_number, "
                    " t_start_ms, t_answer_ms, t_end_ms, duration_ms, final_code, "
                    " created_at) VALUES "
                    "(:cid, 'out', :uuid, '100', '200', 1000, 1120, 61120, 60000, "
                    " 200, '2026-06-10T00:00:00Z')"
                ),
                {"cid": cid, "uuid": f"call-{i}@h"},
            )

    csv_text = loop_engine.records_csv(cid)
    lines = [ln for ln in csv_text.splitlines() if ln.strip()]
    assert lines[0].startswith("id,campaign_id,direction,")
    assert len(lines) == 3  # header + 2 rows
    assert any("call-0@h" in ln for ln in lines)
    assert any("call-1@h" in ln for ln in lines)


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
    assert rows[0].upper() == "SEQUENTIAL"
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


def test_api_records_csv(api_client, loop_engine, db):
    """GET /api/loops/{id}/records.csv returns CSV with the seeded rows."""
    start = api_client.post(
        "/api/loops", json={"dest_host": "9.9.9.9", "duration_s": 1},
    )
    cid = start.json()["campaign"]["id"]

    from sqlalchemy import text
    with db.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO call_records "
                "(campaign_id, direction, call_uuid, a_number, b_number, "
                " duration_ms, final_code, created_at) VALUES "
                "(:cid, 'out', 'x@h', '100', '200', 60000, 200, '2026-06-10T00:00:00Z')"
            ),
            {"cid": cid},
        )

    resp = api_client.get(f"/api/loops/{cid}/records.csv")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    lines = [ln for ln in resp.text.splitlines() if ln.strip()]
    assert lines[0].startswith("id,campaign_id,direction,")
    assert any("x@h" in ln for ln in lines)


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
    # Above the per-campaign channel cap (default 100).
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


def test_records_csv_quotes_comma_and_defangs_formula(loop_engine, db):
    """CSV export quotes a comma field and de-fangs a leading '=' (injection)."""
    campaign = loop_engine.start_campaign(dest_host="9.9.9.9", duration_s=1)
    cid = campaign["id"]

    from sqlalchemy import text
    with db.engine.begin() as conn:
        # a_number carries a formula-injection payload; b_number embeds a comma.
        conn.execute(
            text(
                "INSERT INTO call_records "
                "(campaign_id, direction, call_uuid, a_number, b_number, "
                " source_ip, duration_ms, final_code, created_at) VALUES "
                "(:cid, 'out', 'evil@h', :a, :b, :ip, 1000, 200, "
                " '2026-06-10T00:00:00Z')"
            ),
            {
                "cid": cid,
                "a": "=cmd|'/c calc'!A0",   # formula injection
                "b": "100,200",             # contains a comma
                "ip": "@SUM(1+1)",          # another formula lead
            },
        )

    csv_text = loop_engine.records_csv(cid)

    # Parse it back with the stdlib reader — proves the quoting is well-formed.
    import csv as _csv
    import io as _io
    rows = list(_csv.reader(_io.StringIO(csv_text)))
    header = rows[0]
    data = rows[1]
    record = dict(zip(header, data))

    # The comma field round-trips as ONE cell (was quoted, not split).
    assert record["b_number"] == "100,200"
    # The formula leads are de-fanged with a leading apostrophe.
    assert record["a_number"].startswith("'="), record["a_number"]
    assert record["source_ip"].startswith("'@"), record["source_ip"]


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
