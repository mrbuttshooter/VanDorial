"""
Call-record retention tests (design §5 retention, §7 stage 10).

The retention job's defining property — the reason it is its own module — is
that it is INTERVAL-GATED, never per-iteration: it must not recreate the old
"sigma" (NetAxis) DELETE-on-every-tick storm. These tests pin exactly that,
with no real SIPp/Docker/Linux (SQLite only, synthetic rows inserted directly):

  * a prune removes rows older than the retention window and KEEPS recent ones;
  * a second prune attempted before the interval has elapsed is a NO-OP (the
    storm guard) — this is the load-bearing assertion;
  * the gate is persisted, so a fresh RetentionJob (a "restart") still honors
    the gate and does not DELETE on boot;
  * once the interval HAS elapsed, the prune runs again;
  * retention_days <= 0 disables pruning entirely (never "delete everything").
"""

import datetime

import pytest
from sqlalchemy import text

from gencall.core.retention import JOB_NAME, RetentionJob
from gencall.db.migrations import apply_migrations
from gencall.db.models import Database


# ── fixtures / helpers ──────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    """Temp SQLite Database with ORM tables + plain SQL migrations applied."""
    database = Database(f"sqlite:///{tmp_path / 'retention.db'}")
    database.create_tables()
    apply_migrations(database.engine)
    return database


def _iso_days_ago(days):
    """ISO-8601 UTC timestamp ``days`` in the past (matches created_at format)."""
    return (datetime.datetime.now(datetime.UTC)
            - datetime.timedelta(days=days)).isoformat()


def _insert_record(db, *, call_uuid, created_at, direction="out"):
    """Insert one minimal call_record with an explicit created_at."""
    with db.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO call_records "
                "(campaign_id, direction, call_uuid, duration_ms, created_at) "
                "VALUES (:cid, :dir, :uuid, 0, :created_at)"
            ),
            {"cid": "c1", "dir": direction, "uuid": call_uuid,
             "created_at": created_at},
        )


def _count(db):
    with db.engine.connect() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM call_records")).scalar()


def _gate_epoch(db):
    with db.engine.connect() as conn:
        row = conn.execute(
            text("SELECT last_run_at FROM retention_runs WHERE job_name = :j"),
            {"j": JOB_NAME},
        ).fetchone()
    return float(row[0]) if row else None


# ── tests ───────────────────────────────────────────────────────────────────


def test_prune_deletes_old_keeps_recent(db):
    """A prune removes rows past the retention window and keeps recent ones."""
    _insert_record(db, call_uuid="old1", created_at=_iso_days_ago(40))
    _insert_record(db, call_uuid="old2", created_at=_iso_days_ago(31))
    _insert_record(db, call_uuid="fresh1", created_at=_iso_days_ago(5))
    _insert_record(db, call_uuid="fresh2", created_at=_iso_days_ago(0))

    job = RetentionJob(db=db, retention_days=30, min_interval_s=24 * 3600)
    deleted = job.run_once()

    assert deleted == 2
    assert _count(db) == 2
    with db.engine.connect() as conn:
        remaining = {
            r[0]
            for r in conn.execute(text("SELECT call_uuid FROM call_records"))
        }
    assert remaining == {"fresh1", "fresh2"}


def test_second_prune_within_interval_is_noop(db):
    """THE storm guard: a prune before the interval elapses deletes nothing.

    This is the assertion that proves we did not rebuild sigma's per-iteration
    DELETE storm. After the first prune advances the (persisted) gate, an
    immediate second run must skip the DELETE entirely — even though there is
    now a newly-aged row that *would* otherwise qualify.
    """
    _insert_record(db, call_uuid="old1", created_at=_iso_days_ago(40))
    _insert_record(db, call_uuid="fresh", created_at=_iso_days_ago(1))

    job = RetentionJob(db=db, retention_days=30, min_interval_s=24 * 3600)

    assert job.run_once() == 1          # first prune removes the aged row
    assert job.due() is False           # gate now closed for the interval

    # A row that is already past the window exists, but the interval has NOT
    # elapsed, so a second pass must be a strict no-op.
    _insert_record(db, call_uuid="old2", created_at=_iso_days_ago(35))
    assert job.run_once() == 0
    assert _count(db) == 2              # old2 + fresh both survive the gate


def test_gate_persists_across_restart(db):
    """The gate is persisted, so a fresh job ("restart") honors it.

    A crash/restart loop must not be able to DELETE on every boot — the gate is
    read from retention_runs, not process memory.
    """
    _insert_record(db, call_uuid="old1", created_at=_iso_days_ago(40))
    first = RetentionJob(db=db, retention_days=30, min_interval_s=24 * 3600)
    assert first.run_once() == 1
    gate_after = _gate_epoch(db)
    assert gate_after and gate_after > 0

    # Simulate a process restart: a brand-new job against the same DB.
    _insert_record(db, call_uuid="old2", created_at=_iso_days_ago(40))
    restarted = RetentionJob(db=db, retention_days=30, min_interval_s=24 * 3600)
    assert restarted.due() is False     # persisted gate still closed
    assert restarted.run_once() == 0    # no DELETE on "boot"
    assert _gate_epoch(db) == gate_after  # gate untouched by the skipped pass


def test_prune_runs_again_after_interval_elapses(db):
    """Once the interval HAS elapsed, the next pass prunes again.

    Rather than sleep, we backdate the persisted gate timestamp so the job sees
    the interval as already elapsed — exercising the same persisted-gate read
    that governs production without a wall-clock wait.
    """
    _insert_record(db, call_uuid="old1", created_at=_iso_days_ago(40))
    job = RetentionJob(db=db, retention_days=30, min_interval_s=24 * 3600)
    assert job.run_once() == 1
    assert job.due() is False           # gate just closed

    # Backdate the gate by more than the interval (simulate a day passing).
    with db.engine.begin() as conn:
        conn.execute(
            text("UPDATE retention_runs SET last_run_at = last_run_at - :age "
                 "WHERE job_name = :j"),
            {"age": 24 * 3600 + 60, "j": JOB_NAME},
        )

    _insert_record(db, call_uuid="old2", created_at=_iso_days_ago(40))
    assert job.due() is True            # interval now elapsed
    assert job.run_once() == 1
    assert _count(db) == 0


def test_disabled_when_retention_days_zero(db):
    """retention_days <= 0 disables pruning — never a 'delete everything'."""
    _insert_record(db, call_uuid="old1", created_at=_iso_days_ago(999))
    job = RetentionJob(db=db, retention_days=0, min_interval_s=24 * 3600)

    assert job.due() is False
    assert job.run_once() == 0
    assert job.run_once(force=True) == 0   # force bypasses only the time gate
    assert _count(db) == 1


def test_force_bypasses_time_gate_only(db):
    """force=True ignores the interval but still only deletes OLD rows."""
    _insert_record(db, call_uuid="old1", created_at=_iso_days_ago(40))
    _insert_record(db, call_uuid="fresh", created_at=_iso_days_ago(2))
    job = RetentionJob(db=db, retention_days=30, min_interval_s=24 * 3600)

    assert job.run_once() == 1           # gate now closed
    _insert_record(db, call_uuid="old2", created_at=_iso_days_ago(40))
    # Normal pass is gated off; forced pass runs but still respects the window.
    assert job.run_once() == 0
    assert job.run_once(force=True) == 1
    assert _count(db) == 1               # only "fresh" remains
