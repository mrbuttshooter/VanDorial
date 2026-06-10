"""
Reliability-stage tests (design §4.5 / §5): the managed-process registry,
engine PID tracking, shutdown stop_all, startup reconciliation, and the
single-worker guard.

These run with no real SIPp/Docker/Linux: the stub `sipp` (tests/stubs) is wired
through the REAL SIPpEngine via the `stub_sipp` fixture, and a temp SQLite DB is
created with the plain SQL migrations applied. On Windows only the terminate()
stop path and taskkill reconciliation are exercised (CI/Linux covers SIGUSR1).
"""

import os
import time

import pytest

from gencall.core.process_registry import ProcessRegistry, cmdline_hash
from gencall.core.sipp_engine import (
    SIPpEngine,
    SIPpInstance,
    SIPpMode,
    SIPpState,
    SIPpTransport,
)
from gencall.db.migrations import apply_migrations
from gencall.db.models import Database


def _wait_until(predicate, timeout=15.0, interval=0.1):
    deadline = time.time() + timeout
    val = predicate()
    while not val and time.time() < deadline:
        time.sleep(interval)
        val = predicate()
    return val


@pytest.fixture
def db(tmp_path):
    """A temp SQLite Database with ORM tables + plain SQL migrations applied."""
    db_path = tmp_path / "reg.db"
    database = Database(f"sqlite:///{db_path}")
    database.create_tables()
    apply_migrations(database.engine)
    return database


def _make_instance(stub_env, **overrides):
    params = dict(
        id="reg-1",
        scenario_file="dummy.xml",
        remote_host="127.0.0.1",
        remote_port=5060,
        local_port=5061,
        mode=SIPpMode.UAC,
        transport=SIPpTransport.UDP,
        call_rate=20.0,
        call_limit=10,
        max_calls=0,
    )
    params.update(overrides)
    return SIPpInstance(**params)


def _registered_pids(registry):
    return {int(r["pid"]) for r in registry.list_all()}


# ── 1. PID recorded on start & cleared on stop ──────────────────────────────

def test_pid_recorded_on_start_and_cleared_on_stop(stub_sipp, db):
    registry = ProcessRegistry(db=db)
    engine = SIPpEngine(stub_sipp.config, registry=registry)
    inst = _make_instance(stub_sipp, id="reg-start-stop", campaign_id="camp-1")

    assert engine.start_instance(inst) is True
    pid = inst._process.pid
    try:
        # PID is in the registry with the right role/campaign.
        rows = registry.list_all()
        match = [r for r in rows if int(r["pid"]) == pid]
        assert match, f"PID {pid} was not recorded on start"
        assert match[0]["role"] == "uac"
        assert match[0]["campaign_id"] == "camp-1"
        assert match[0]["cmdline_hash"]
    finally:
        assert engine.stop_instance(inst.id) is True

    # After a clean stop the PID is gone from the registry.
    assert pid not in _registered_pids(registry), "PID not cleared on stop"


# ── 2. stop_all kills everything (and clears the registry) ──────────────────

def test_stop_all_kills_everything(stub_sipp, db):
    registry = ProcessRegistry(db=db)
    engine = SIPpEngine(stub_sipp.config, registry=registry)

    insts = []
    for i in range(3):
        inst = _make_instance(
            stub_sipp, id=f"reg-all-{i}", local_port=5061 + i, campaign_id=f"c{i}"
        )
        assert engine.start_instance(inst) is True
        insts.append(inst)

    procs = [i._process for i in insts]
    assert all(p.poll() is None for p in procs)
    assert len(_registered_pids(registry)) == 3

    engine.stop_all()

    # Every process is reaped and every PID is cleared from the registry.
    for p in procs:
        exited = _wait_until(lambda p=p: p.poll() is not None, timeout=10.0)
        assert exited, "stop_all left an orphaned process"
    for inst in insts:
        assert inst.state == SIPpState.STOPPED
    assert _registered_pids(registry) == set(), "stop_all left stale registry rows"


# ── 3. startup reconciliation kills a registered stray + marks interrupted ──

def test_reconcile_kills_registered_stray_and_marks_campaign(stub_sipp, db):
    """A stray PID from a 'previous run' is killed on boot and its campaign
    is marked interrupted; the registry row is cleared."""
    from sqlalchemy import text

    # Seed a running campaign that the stray belongs to.
    campaign_id = "camp-interrupted"
    with db.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO loop_campaigns (id, name, status) "
                "VALUES (:id, :name, 'running')"
            ),
            {"id": campaign_id, "name": "stray-owner"},
        )

    # Spawn a real stub process directly (simulating a survivor of a crash) and
    # register it as if a previous GenCall run had recorded it.
    registry = ProcessRegistry(db=db)
    engine = SIPpEngine(stub_sipp.config, registry=registry)
    inst = _make_instance(
        stub_sipp, id="reg-stray", call_rate=5.0, campaign_id=campaign_id
    )
    assert engine.start_instance(inst) is True
    stray = inst._process
    stray_pid = stray.pid
    assert stray.poll() is None

    # Simulate a fresh boot: a brand-new registry over the same DB sees the row.
    fresh_registry = ProcessRegistry(db=db)
    assert stray_pid in _registered_pids(fresh_registry)

    summary = fresh_registry.reconcile()

    assert stray_pid in summary["killed"], "stray PID was not killed by reconcile"
    assert campaign_id in summary["interrupted_campaigns"]

    # The stray really died.
    exited = _wait_until(lambda: stray.poll() is not None, timeout=10.0)
    assert exited, "reconcile did not actually kill the stray process"

    # Registry row cleared, campaign marked interrupted in DB.
    assert stray_pid not in _registered_pids(fresh_registry)
    with db.engine.connect() as conn:
        status = conn.execute(
            text("SELECT status FROM loop_campaigns WHERE id = :id"),
            {"id": campaign_id},
        ).scalar()
    assert status == "interrupted"


def test_reconcile_skips_reused_pid(db):
    """PID-reuse guard: a recorded PID whose live cmdline no longer matches the
    recorded hash is NOT killed (kill_fn is never called for it)."""
    registry = ProcessRegistry(db=db)
    # Record this very test process's PID but with a bogus cmdline hash, so the
    # PID is alive yet the hash cannot match -> must be skipped, not killed.
    my_pid = os.getpid()
    registry.record(
        pid=my_pid, role="uac",
        cmdline_hash_value="deadbeef" * 8,  # 64 hex chars, won't match reality
        campaign_id=None,
    )

    killed_calls = []
    summary = registry.reconcile(kill_fn=lambda pid: killed_calls.append(pid))

    assert my_pid not in summary["killed"]
    assert my_pid in summary["skipped"]
    assert killed_calls == [], "reconcile killed a PID-reuse candidate"
    # Stale row is still cleared.
    assert my_pid not in _registered_pids(registry)


def test_reconcile_ignores_dead_pid(db):
    """A recorded PID that is no longer alive is skipped (not killed) and the
    stale row is cleared."""
    registry = ProcessRegistry(db=db)
    # A PID that is essentially never alive.
    dead_pid = 999999
    registry.record(
        pid=dead_pid, role="uas", cmdline_hash_value="a" * 64, campaign_id=None
    )

    killed_calls = []
    summary = registry.reconcile(kill_fn=lambda pid: killed_calls.append(pid))

    assert dead_pid not in summary["killed"]
    assert killed_calls == []
    assert dead_pid not in _registered_pids(registry)


# ── JSON fallback ───────────────────────────────────────────────────────────

def test_json_fallback_when_db_down(tmp_path):
    """With no DB, record/list/clear round-trip through the JSON fallback file."""
    fallback = tmp_path / "managed.json"
    registry = ProcessRegistry(db=None, fallback_path=str(fallback))

    registry.record(pid=4242, role="uac", cmdline_hash_value="x" * 64, campaign_id="cf")
    assert os.path.exists(fallback)
    rows = registry.list_all()
    assert any(int(r["pid"]) == 4242 for r in rows)

    registry.clear(4242)
    assert all(int(r["pid"]) != 4242 for r in registry.list_all())


def test_cmdline_hash_stable_and_distinct():
    a = cmdline_hash(["sipp", "-sf", "x.xml", "127.0.0.1:5060"])
    b = cmdline_hash(["sipp", "-sf", "x.xml", "127.0.0.1:5060"])
    c = cmdline_hash(["sipp", "-sf", "y.xml", "127.0.0.1:5060"])
    assert a == b
    assert a != c
    assert len(a) == 64


# ── 4. --workers > 1 rejected ───────────────────────────────────────────────

def test_workers_gt_one_rejected(monkeypatch):
    """main() refuses --workers 2 with a clear error (single-process guard)."""
    import gencall.main as gmain

    monkeypatch.setattr(
        "sys.argv", ["gencall", "--workers", "2"]
    )
    with pytest.raises(SystemExit) as exc:
        gmain.main()
    # argparse error() exits with code 2.
    assert exc.value.code == 2


def test_workers_one_allowed_parses(monkeypatch):
    """--workers 1 passes the guard (argparse does not bail before run)."""
    import gencall.main as gmain

    # Stop before uvicorn.run / app creation by making create_app blow up in a
    # way we can detect — easiest is to assert the guard didn't fire by checking
    # parse path: patch create_app to raise a sentinel and uvicorn.run unused.
    sentinel = RuntimeError("reached-app-build")

    def _boom(*a, **k):
        raise sentinel

    monkeypatch.setattr(gmain, "create_app", _boom)
    monkeypatch.setattr("sys.argv", ["gencall", "--workers", "1"])
    with pytest.raises(RuntimeError) as exc:
        gmain.main()
    assert exc.value is sentinel  # got past the workers guard
