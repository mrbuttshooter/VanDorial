"""
Call-record retention job (design §5 retention, §7 stage 10).

``call_records`` is the growth table: at 50 concurrent loops / ~3-min calls it
accrues ≈ 24k rows/day/direction (§5). This module prunes rows older than a
configurable window (config ``[retention] call_records_days``, default 30) so a
single 4 GB box never fills its disk.

The ONE hard rule, stated twice in the spec, is the reason this is its own
module: the prune is **INTERVAL-GATED, never per-iteration**. The old "sigma"
(NetAxis) build issued a ``DELETE`` on *every* scheduler tick — a self-inflicted
DELETE storm that pinned the DB. We must not recreate it. So:

  * The job runs at most once per ``min_interval_s`` (config
    ``[retention] interval_hours``, default 24 h). At the top of every pass it
    reads the last actual-prune timestamp from ``retention_runs`` and returns
    immediately (no DELETE) if the interval has not elapsed.
  * That timestamp is **persisted** (not in-memory), so a crash/restart loop
    cannot bypass the gate and DELETE on every boot.
  * A retention window of 0 (or negative) disables pruning entirely — the job
    is a no-op, never a "delete everything".

Control-plane only, like the rest of GenCall: a single throttled DELETE per
elapsed interval, and the optional background thread sleeps >= 1 s between gate
checks (no busy loop, per this codebase's standard). The cutoff is computed
against ``call_records.created_at`` (ISO-8601 UTC, which sorts lexically) so the
comparison is a single indexed-free range scan, not a per-row loop.
"""

import datetime
import logging
import threading

logger = logging.getLogger("gencall.retention")

# Logical job id keyed in the retention_runs gate table. One job today; the
# table is keyed so a second retention target (e.g. loop_stats) could be added.
JOB_NAME = "call_records"

# Floor for the background thread's gate-check sleep. The actual prune cadence is
# governed by ``min_interval_s`` (hours); this is only how often the thread wakes
# to re-check the gate. Per this codebase's standard, no poll loop sleeps < 1 s.
MIN_CHECK_SLEEP_S = 1.0

# Default retention window and interval (overridden from config by the caller).
DEFAULT_RETENTION_DAYS = 30
DEFAULT_INTERVAL_HOURS = 24


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _now_epoch():
    return _now().timestamp()


def _now_iso():
    return _now().isoformat()


class RetentionJob:
    """Interval-gated pruner for ``call_records`` (design §5).

    Parameters
    ----------
    db:
        A ``gencall.db.models.Database`` (or None for a disabled no-op job).
    retention_days:
        Delete records whose ``created_at`` is older than this many days. A
        value <= 0 disables pruning (the job becomes a no-op).
    min_interval_s:
        Minimum seconds between two *actual* prunes — the interval gate. A prune
        attempted before this has elapsed since the last one is skipped. This is
        the storm guard; it is enforced against the persisted ``retention_runs``
        timestamp, not a process-local clock.
    """

    def __init__(self, db=None, retention_days=DEFAULT_RETENTION_DAYS,
                 min_interval_s=DEFAULT_INTERVAL_HOURS * 3600):
        self.db = db
        self.retention_days = int(retention_days)
        # The gate interval. Never let it be negative; 0 would mean "no gate",
        # which is exactly the storm we are preventing, so floor it at 1 s.
        self.min_interval_s = max(float(min_interval_s), 1.0)
        self._stop = threading.Event()
        self._thread = None

    # ── gate state (persisted in retention_runs) ─────────────────────────────

    def _last_run_epoch(self):
        """Epoch seconds of the last actual prune (0.0 if never run / no DB)."""
        if self.db is None:
            return 0.0
        from sqlalchemy import text

        try:
            with self.db.engine.connect() as conn:
                row = conn.execute(
                    text("SELECT last_run_at FROM retention_runs "
                         "WHERE job_name = :j"),
                    {"j": JOB_NAME},
                ).fetchone()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Could not read retention gate: %s", e)
            return 0.0
        return float(row[0]) if row and row[0] is not None else 0.0

    def _record_run(self, conn, deleted, run_epoch):
        """Upsert the gate row with the timestamp of this actual prune.

        Written inside the same transaction as the DELETE so the gate advances
        if and only if the prune committed — a rolled-back DELETE never moves the
        gate forward, and a committed one always does (no double-prune window).
        """
        from sqlalchemy import text

        existing = conn.execute(
            text("SELECT job_name FROM retention_runs WHERE job_name = :j"),
            {"j": JOB_NAME},
        ).fetchone()
        params = {
            "j": JOB_NAME,
            "ts": run_epoch,
            "del": int(deleted),
            "iso": _now_iso(),
        }
        if existing:
            conn.execute(
                text("UPDATE retention_runs SET last_run_at = :ts, "
                     "last_deleted = :del, updated_at = :iso "
                     "WHERE job_name = :j"),
                params,
            )
        else:
            conn.execute(
                text("INSERT INTO retention_runs "
                     "(job_name, last_run_at, last_deleted, updated_at) "
                     "VALUES (:j, :ts, :del, :iso)"),
                params,
            )

    # ── prune ────────────────────────────────────────────────────────────────

    def due(self, now_epoch=None):
        """True if the interval has elapsed since the last actual prune.

        This is the storm guard, read from the persisted gate so it holds across
        restarts. With pruning disabled (``retention_days <= 0``) it is always
        False — a disabled job is never "due".
        """
        if self.db is None or self.retention_days <= 0:
            return False
        now = _now_epoch() if now_epoch is None else now_epoch
        return (now - self._last_run_epoch()) >= self.min_interval_s

    def run_once(self, force=False):
        """Prune once IF the interval gate allows it; return rows deleted.

        Returns the number of ``call_records`` rows removed, or 0 when the job is
        disabled, has no DB, or — the storm guard — is not yet due. The DELETE
        and the gate update share one transaction so the gate advances exactly
        when the prune commits.

        ``force=True`` bypasses only the *time* gate (for an operator-triggered
        prune / tests); it still respects ``retention_days <= 0`` (disabled).
        It never bypasses the retention window itself — old rows only.
        """
        if self.db is None or self.retention_days <= 0:
            return 0

        now_epoch = _now_epoch()
        if not force and not self.due(now_epoch=now_epoch):
            # NOT due — skip the DELETE entirely. This is the line that keeps us
            # from rebuilding sigma's per-iteration DELETE storm.
            logger.debug("Retention prune skipped: interval not elapsed")
            return 0

        # created_at is ISO-8601 UTC (sorts lexically), so the cutoff is just an
        # ISO string and the predicate is a plain range — no per-row work.
        cutoff = _now() - datetime.timedelta(days=self.retention_days)
        cutoff_iso = cutoff.isoformat()

        from sqlalchemy import text

        deleted = 0
        try:
            with self.db.engine.begin() as conn:
                result = conn.execute(
                    text("DELETE FROM call_records WHERE created_at < :cutoff"),
                    {"cutoff": cutoff_iso},
                )
                deleted = result.rowcount or 0
                # Advance the persisted gate in the SAME transaction.
                self._record_run(conn, deleted, now_epoch)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Retention prune failed: %s", e)
            return 0

        if deleted:
            logger.info("Retention pruned %d call_records older than %s",
                        deleted, cutoff_iso)
        return deleted

    # ── background loop (gate-checked, sleeps >= 1 s — no busy poll) ──────────

    def start(self):
        """Start the background retention thread (idempotent).

        The thread only *checks the gate* on each wake; the actual prune still
        runs at most once per ``min_interval_s``. The check sleep is floored at
        ``MIN_CHECK_SLEEP_S`` but capped at the interval so we never busy-wake.
        """
        if self.db is None or self.retention_days <= 0:
            logger.info("Retention job disabled (no DB or retention_days <= 0)")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="retention-job"
        )
        self._thread.start()

    def stop(self, timeout=5.0):
        """Signal the loop to exit and join the thread."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run(self):
        # Wake at most once per interval (and never more often than every 1 s) to
        # re-check the gate; run_once() itself enforces the real prune cadence.
        sleep_s = max(MIN_CHECK_SLEEP_S, min(self.min_interval_s, 3600.0))
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("Retention pass failed: %s", e)
            self._stop.wait(sleep_s)


def build_from_config(config, db):
    """Construct a RetentionJob from a ``gencall.core.config.Config``.

    Reads ``[retention] call_records_days`` (default 30) and
    ``[retention] interval_hours`` (default 24). Centralizing this keeps the
    config keys in one place for the engine wiring and the tests.
    """
    days = config.retention_call_records_days
    interval_s = config.retention_interval_hours * 3600
    return RetentionJob(db=db, retention_days=days, min_interval_s=interval_s)
