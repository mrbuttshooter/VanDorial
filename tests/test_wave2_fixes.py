"""Regression tests for the Wave 2 bug-hunt fixes (improve/gencall-3.0-loop).

All non-call-path: CDR billed-minute integrity, console session revocation, the
stats snapshot race guard. (The two cross-type fleet-stop guards live in
tests/test_controller.py where the controller fixture is defined.)
"""

import pytest

from gencall.core.config import Config


# ─── #1a record_max_age_s default must exceed the answered-call ceiling ────────


def test_record_max_age_default_tracks_answered_ceiling():
    """The staleness sweep default must sit above loops_answered_max_duration_s so
    a legitimately long answered call is never force-evicted mid-call (which used
    to re-ingest it as a 0-second/code-0 record and undercount billed minutes)."""
    Config.reset()
    try:
        c = Config()
        # The invariant that prevents the billed-minute undercount: the staleness
        # sweep must never fire before the answered ceiling, whether the value
        # comes from the shipped cfg or the computed code default.
        assert c.loops_record_max_age_s >= c.loops_answered_max_duration_s
    finally:
        Config.reset()

    # And with NO cfg override, the code default tracks the answered ceiling
    # instead of the old fixed 1800 that sat below it.
    import tempfile
    import os
    empty = tempfile.NamedTemporaryFile(suffix=".cfg", delete=False, mode="w")
    empty.write("[loops]\nanswered_max_duration_s = 6000\n")
    empty.close()
    Config.reset()
    try:
        c = Config(path=empty.name)
        assert c.loops_record_max_age_s == max(1800, 6000 + 300)
    finally:
        Config.reset()
        os.unlink(empty.name)


# ─── #1b _persist must merge, not clobber a good answered row ──────────────────


def _fetch_one(db):
    from sqlalchemy import text
    with db.engine.connect() as conn:
        r = conn.execute(text(
            "SELECT t_answer_ms, t_end_ms, duration_ms, final_code "
            "FROM call_records ORDER BY id")).fetchone()
    return dict(zip(("t_answer_ms", "t_end_ms", "duration_ms", "final_code"), r))


def test_persist_partial_reingest_does_not_null_answered_row(tmp_path):
    """A later BYE-only re-ingest (final_code=0, duration=0, no t_answer) must NOT
    overwrite an already-persisted answered call's final_code/duration."""
    from gencall.core.call_records import CallRecordParser
    from gencall.db.models import Database
    from gencall.db.migrations import apply_migrations

    db = Database(f"sqlite:///{tmp_path / 'cdr.db'}")
    db.create_tables()
    apply_migrations(db.engine)
    p = CallRecordParser(db=db)

    key = {"campaign_id": "camp1", "direction": "out", "call_uuid": "u1",
           "a_number": "111", "b_number": "222", "source_ip": "10.0.0.1"}
    # First pass: a good answered record (final_code=200, real duration).
    p._persist({**key, "t_start_ms": 1000, "t_answer_ms": 1100,
                "t_end_ms": 61100, "duration_ms": 60000, "final_code": 200})
    # Later partial re-ingest built from a BYE line alone: no answer data.
    p._persist({**key, "t_start_ms": None, "t_answer_ms": None,
                "t_end_ms": 61100, "duration_ms": 0, "final_code": 0})

    row = _fetch_one(db)
    assert row["final_code"] == 200          # not clobbered to 0
    assert row["duration_ms"] == 60000       # not clobbered to 0
    assert row["t_answer_ms"] == 1100        # not nulled


def test_persist_still_updates_with_better_values(tmp_path):
    """Merge guard must not block a legitimate improving update (0/NULL -> real)."""
    from gencall.core.call_records import CallRecordParser
    from gencall.db.models import Database
    from gencall.db.migrations import apply_migrations

    db = Database(f"sqlite:///{tmp_path / 'cdr2.db'}")
    db.create_tables()
    apply_migrations(db.engine)
    p = CallRecordParser(db=db)

    key = {"campaign_id": "c", "direction": "out", "call_uuid": "u2",
           "a_number": "1", "b_number": "2", "source_ip": "10.0.0.1"}
    # Ringing-only first (no answer yet).
    p._persist({**key, "t_start_ms": 1000, "t_answer_ms": None,
                "t_end_ms": None, "duration_ms": 0, "final_code": None})
    # Then the answer completes it.
    p._persist({**key, "t_start_ms": 1000, "t_answer_ms": 1100,
                "t_end_ms": 61100, "duration_ms": 60000, "final_code": 200})

    row = _fetch_one(db)
    assert row["final_code"] == 200 and row["duration_ms"] == 60000
    assert row["t_answer_ms"] == 1100


# ─── #2 deleting / re-passwording a user revokes live sessions ─────────────────


@pytest.fixture
def authdb(tmp_path):
    from gencall.db.models import Database
    d = Database(f"sqlite:///{tmp_path / 'auth.db'}")
    d.create_tables()
    return d


def test_delete_user_revokes_active_sessions(authdb):
    from gencall.core.auth_users import UserManager, SessionManager
    um, sm = UserManager(authdb), SessionManager(authdb)
    u = um.create_user("alice", "supersecret")
    tok, _ = sm.create(u["id"], "alice")
    assert sm.validate(tok) is not None

    assert um.delete_user(u["id"]) is True
    assert sm.validate(tok) is None          # session revoked with the account


def test_set_password_revokes_other_sessions(authdb):
    from gencall.core.auth_users import UserManager, SessionManager
    um, sm = UserManager(authdb), SessionManager(authdb)
    u = um.create_user("bob", "supersecret")
    tok, _ = sm.create(u["id"], "bob")
    assert sm.validate(tok) is not None

    assert um.set_password(u["id"], "brandnewsecret") is True
    assert sm.validate(tok) is None          # old session invalidated by reset


# ─── #3 stats snapshot iterates a copy (race guard) ────────────────────────────


class _FakeStats:
    total_calls = 5
    successful_calls = 4
    failed_calls = 1
    current_calls = 2
    calls_per_second = 1.5
    avg_response_time_ms = 12.0


class _FakeState:
    value = "running"


class _FakeInstance:
    state = _FakeState()
    stats = _FakeStats()


def test_stats_collect_aggregates_running_instance():
    """Sanity: _collect reads a running instance's stats without error."""
    from gencall.core.stats import StatsEngine

    class _FakeEngine:
        instances = {"a": _FakeInstance()}

    se = StatsEngine()
    se.set_engine(_FakeEngine())
    se._collect()
    assert se.get_current()["total_calls"] == 5


def test_stats_collect_iterates_a_snapshot_copy():
    """Guard the race fix: _collect must iterate list(...instances.values()), not
    the live dict view — otherwise a concurrent add/remove drops the snapshot."""
    import inspect
    from gencall.core import stats
    src = inspect.getsource(stats.StatsEngine._collect)
    assert "list(self._sipp_engine.instances.values())" in src
