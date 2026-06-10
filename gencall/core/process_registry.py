"""
Managed-process registry (design §4.5 / §5).

Every SIPp process GenCall spawns is recorded here at spawn time — PID, role,
campaign id, and a SHA-256 of the exact argv (``cmdline_hash``) — so we can:

  * on clean stop/remove, forget the PID;
  * on FastAPI lifespan shutdown, stop everything (the engine does the killing,
    the registry just no longer tracks it);
  * on startup, *reconcile*: any recorded PID that is still alive AND whose
    current cmdline still hashes to what we recorded gets killed (it is a stray
    dialer from a crashed/killed previous run), and its campaign is marked
    ``interrupted``. The cmdline_hash guard means a recycled PID now running an
    unrelated process is left alone.

Storage is the ``managed_processes`` DB table (created by the plain SQL
migration in ``gencall/db/migrations``). If the DB is unavailable at record time
we fall back to a JSON file so a crash still leaves a trail for reconciliation.

No busy loops, no per-call work: this is touched only on spawn, stop, shutdown,
and once at boot.
"""

import datetime
import hashlib
import json
import logging
import os
import signal
import subprocess
import tempfile
import threading

logger = logging.getLogger("gencall.registry")

# os.kill(pid, 0) liveness + os.kill(pid, SIGKILL) are POSIX. On Windows we shell
# out to tasklist/taskkill. Detect once so behavior on Linux is unchanged.
_IS_WINDOWS = os.name == "nt"


def cmdline_hash(cmd):
    """SHA-256 of an argv list — the PID-reuse guard key.

    ``cmd`` is the list[str] argv (as built by SIPpInstance.build_command). The
    hash is over the space-joined argv so the same launch always hashes the same
    and a different process on a recycled PID hashes differently.
    """
    joined = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
    return hashlib.sha256(joined.encode("utf-8", errors="replace")).hexdigest()


def _now_iso():
    return datetime.datetime.now(datetime.UTC).isoformat()


def _default_fallback_path():
    """JSON-fallback file path used when the DB is down at record time."""
    return os.path.join(tempfile.gettempdir(), "gencall_managed_processes.json")


class ProcessRegistry:
    """Tracks spawned SIPp PIDs in the DB (with a JSON-file fallback)."""

    def __init__(self, db=None, fallback_path=None):
        # ``db`` is a gencall.db.models.Database (or None to run JSON-only).
        self.db = db
        self.fallback_path = fallback_path or _default_fallback_path()
        self._lock = threading.Lock()

    # ── record / clear ──────────────────────────────────────────────────────

    def record(self, pid, role, cmdline_hash_value, campaign_id=None):
        """Record a spawned PID. Writes to the DB; on any DB error, JSON file."""
        row = {
            "pid": int(pid),
            "role": str(role),
            "campaign_id": campaign_id,
            "cmdline_hash": cmdline_hash_value,
            "spawned_at": _now_iso(),
        }
        if self._db_record(row):
            return True
        # DB unavailable / failed — keep a trail so reconciliation still works.
        self._fallback_record(row)
        return False

    def clear(self, pid):
        """Forget a PID (process stopped/removed). Best-effort in both stores."""
        self._db_clear(pid)
        self._fallback_clear(pid)

    def list_all(self):
        """Return all recorded rows (DB preferred, else JSON fallback)."""
        rows = self._db_list()
        if rows is not None:
            return rows
        return self._fallback_list()

    # ── reconciliation ──────────────────────────────────────────────────────

    def reconcile(self, kill_fn=None):
        """Kill recorded strays and mark their campaigns interrupted.

        For each recorded PID: if it is still alive and its current cmdline still
        hashes to the recorded ``cmdline_hash`` (PID-reuse guard), kill it. Then
        clear the row regardless (dead PIDs are stale entries to forget). Returns
        a summary dict: killed PIDs, skipped (reused/dead), interrupted campaigns.

        ``kill_fn`` (pid -> None) is injectable for tests; defaults to a
        cross-platform SIGKILL/taskkill.
        """
        kill_fn = kill_fn or _kill_pid
        killed = []
        skipped = []
        interrupted_campaigns = set()

        for row in self.list_all():
            pid = int(row["pid"])
            recorded_hash = row.get("cmdline_hash", "")
            campaign_id = row.get("campaign_id")

            if _pid_alive(pid) and _cmdline_matches(pid, recorded_hash):
                try:
                    kill_fn(pid)
                    killed.append(pid)
                    if campaign_id:
                        interrupted_campaigns.add(campaign_id)
                    logger.warning(
                        "Reconciliation killed stray SIPp PID %d (role=%s, campaign=%s)",
                        pid, row.get("role"), campaign_id,
                    )
                except Exception as e:  # pragma: no cover - defensive
                    logger.warning("Failed to kill stray PID %d: %s", pid, e)
                    skipped.append(pid)
            else:
                # Dead, or PID reused by an unrelated process — never touch it.
                skipped.append(pid)

            # Either way the row is stale now; forget it.
            self.clear(pid)

        for campaign_id in interrupted_campaigns:
            self._mark_campaign_interrupted(campaign_id)

        if killed:
            logger.warning(
                "Startup reconciliation killed %d stray process(es); "
                "marked %d campaign(s) interrupted",
                len(killed), len(interrupted_campaigns),
            )
        return {
            "killed": killed,
            "skipped": skipped,
            "interrupted_campaigns": sorted(interrupted_campaigns),
        }

    # ── DB-backed store (raw SQL, no ORM dependency) ────────────────────────

    def _db_record(self, row):
        if self.db is None:
            return False
        try:
            from sqlalchemy import text

            with self.db.engine.begin() as conn:
                # Upsert by PID: a recycled PID overwrites the stale entry.
                conn.execute(
                    text("DELETE FROM managed_processes WHERE pid = :pid"),
                    {"pid": row["pid"]},
                )
                conn.execute(
                    text(
                        "INSERT INTO managed_processes "
                        "(pid, role, campaign_id, cmdline_hash, spawned_at) "
                        "VALUES (:pid, :role, :campaign_id, :cmdline_hash, :spawned_at)"
                    ),
                    row,
                )
            return True
        except Exception as e:
            logger.warning("DB record of PID %s failed (using JSON fallback): %s",
                           row.get("pid"), e)
            return False

    def _db_clear(self, pid):
        if self.db is None:
            return
        try:
            from sqlalchemy import text

            with self.db.engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM managed_processes WHERE pid = :pid"),
                    {"pid": int(pid)},
                )
        except Exception as e:
            logger.debug("DB clear of PID %s failed: %s", pid, e)

    def _db_list(self):
        if self.db is None:
            return None
        try:
            from sqlalchemy import text

            with self.db.engine.connect() as conn:
                result = conn.execute(
                    text(
                        "SELECT pid, role, campaign_id, cmdline_hash, spawned_at "
                        "FROM managed_processes"
                    )
                )
                return [
                    {
                        "pid": r[0],
                        "role": r[1],
                        "campaign_id": r[2],
                        "cmdline_hash": r[3],
                        "spawned_at": r[4],
                    }
                    for r in result
                ]
        except Exception as e:
            logger.warning("DB list of managed_processes failed: %s", e)
            return None

    def _mark_campaign_interrupted(self, campaign_id):
        """Best-effort mark a loop campaign 'interrupted' in the DB.

        The ``loop_campaigns`` table arrives in a later stage; until then this is
        a no-op-on-missing-table (the UPDATE simply affects nothing / errors are
        swallowed) so reconciliation never crashes a boot.
        """
        if self.db is None:
            return
        try:
            from sqlalchemy import text

            with self.db.engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE loop_campaigns SET status = 'interrupted' "
                        "WHERE id = :id AND status = 'running'"
                    ),
                    {"id": campaign_id},
                )
        except Exception as e:
            logger.debug(
                "Could not mark campaign %s interrupted (table may not exist yet): %s",
                campaign_id, e,
            )

    # ── JSON fallback store ─────────────────────────────────────────────────

    def _fallback_load(self):
        try:
            with open(self.fallback_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                return data
        except (OSError, ValueError):
            pass
        return []

    def _fallback_save(self, rows):
        try:
            with open(self.fallback_path, "w", encoding="utf-8") as fh:
                json.dump(rows, fh)
        except OSError as e:
            logger.warning("Could not write registry fallback file: %s", e)

    def _fallback_record(self, row):
        with self._lock:
            rows = [r for r in self._fallback_load() if r.get("pid") != row["pid"]]
            rows.append(row)
            self._fallback_save(rows)

    def _fallback_clear(self, pid):
        with self._lock:
            rows = [r for r in self._fallback_load() if r.get("pid") != int(pid)]
            self._fallback_save(rows)

    def _fallback_list(self):
        return self._fallback_load()


# ── cross-platform process helpers ──────────────────────────────────────────

def _pid_alive(pid):
    """True if a process with this PID currently exists."""
    pid = int(pid)
    if pid <= 0:
        return False
    if _IS_WINDOWS:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10,
            )
            return str(pid) in out.stdout
        except (OSError, subprocess.SubprocessError):
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Exists but owned by another user — treat as alive.
            return True
        except OSError:
            return False


def current_cmdline_hash(pid):
    """Public: SHA-256 of the OS-reported cmdline for a live PID, or None.

    Recorded at spawn time so reconciliation compares like-for-like (the live
    cmdline can differ from the argv we passed to Popen — e.g. a launcher that
    hands off to a different program — so hashing the argv would never match
    later).
    """
    return _current_cmdline_hash(pid)


def _current_cmdline_hash(pid):
    """SHA-256 of the running process's current argv, or None if unobtainable.

    On Linux we read /proc/<pid>/cmdline (NUL-separated argv). On Windows we
    query WMIC/CIM for the command line. When we cannot read it we return None,
    and the caller treats that as "can't prove identity" — see _cmdline_matches.
    """
    pid = int(pid)
    if _IS_WINDOWS:
        try:
            out = subprocess.run(
                [
                    "wmic", "process", "where", f"ProcessId={pid}",
                    "get", "CommandLine", "/format:list",
                ],
                capture_output=True, text=True, timeout=10,
            )
            for line in out.stdout.splitlines():
                if line.startswith("CommandLine="):
                    raw = line[len("CommandLine="):].strip()
                    if not raw:
                        return None
                    return hashlib.sha256(
                        raw.encode("utf-8", errors="replace")
                    ).hexdigest()
        except (OSError, subprocess.SubprocessError):
            return None
        return None
    else:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                raw = fh.read()
            # /proc cmdline is NUL-separated and NUL-terminated.
            argv = [p for p in raw.split(b"\x00") if p]
            joined = b" ".join(argv).decode("utf-8", errors="replace")
            return hashlib.sha256(
                joined.encode("utf-8", errors="replace")
            ).hexdigest()
        except OSError:
            return None


def _cmdline_matches(pid, recorded_hash):
    """PID-reuse guard: does the live process still match what we recorded?

    If we can read the current cmdline, require an exact hash match (recycled PID
    running something else won't match → not killed). If we *cannot* read it
    (e.g. Windows without WMIC, or a permission wall), we fall back to trusting
    the registry record so a real stray dialer is still reaped — the recorded
    hash is non-empty and the PID is alive, which is the common crash case. This
    keeps the safety property (kill orphans) while making PID-reuse the only edge
    we can't perfectly distinguish on a locked-down host.
    """
    if not recorded_hash:
        return False
    current = _current_cmdline_hash(pid)
    if current is None:
        return True
    return current == recorded_hash


def _kill_pid(pid):
    """Force-kill a PID, cross-platform."""
    pid = int(pid)
    if _IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True, text=True, timeout=10,
        )
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
