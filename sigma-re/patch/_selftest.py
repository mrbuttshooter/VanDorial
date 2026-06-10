#!/usr/bin/env python3
"""
_selftest.py - offline behavioral test for sigma_patch v3 (no sigma install needed).

Verifies, against a mock Scheduler shaped like the recovered one, the three
guarantees that fix the v2 "calls going 487" regression:

  1. The loop floor caps the scheduler loop's pass rate (~1/floor Hz).
  2. Calls into the wrapped poll methods from ANY OTHER thread are never slept
     (scenario/web threads must not be delayed - that's what CANCELled calls).
  3. monitorScenarios (live-call path) is not wrapped at all by default.
  4. The cleanup/do_cleanup interval gate still works.

Run:  python _selftest.py     -> "SELFTEST: PASS" and exit 0, or details and exit 1.
"""

import os
import sys
import tempfile
import threading
import time
import types

FLOOR = 0.05
CLEANUP_INTERVAL = 0.2

os.environ["SIGMA_PATCH_LOOP_FLOOR"] = str(FLOOR)
os.environ["SIGMA_PATCH_CLEANUP_INTERVAL"] = str(CLEANUP_INTERVAL)
os.environ["SIGMA_PATCH_LOG"] = os.path.join(tempfile.gettempdir(),
                                             "sigma_patch_selftest.log")
os.environ.pop("SIGMA_PATCH_DISABLE", None)
os.environ.pop("SIGMA_PATCH_SCHED_DISABLE", None)
os.environ.pop("SIGMA_PATCH_DRY_RUN", None)
os.environ.pop("SIGMA_PATCH_FLOOR_METHODS", None)

# Re-import fresh so the env above is what the module reads (the .pth may have
# auto-imported it with different settings before we got here).
sys.modules.pop("sigma_patch", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sigma_patch  # noqa: E402


class Scheduler:
    """Mock mirroring the recovered sigma.core.scheduler.Scheduler loop shape."""

    def __init__(self):
        self.counts = {"scan": 0, "monitor": 0, "cleanup_ran": 0}
        self._stop = False

    def run(self):
        while not self._stop:
            self.scanDatabaseScenarios()
            self.checkPlannings()
            self.monitorScenarios()
            self.cleanup()

    def scanDatabaseScenarios(self):
        self.counts["scan"] += 1

    def checkPlannings(self):
        pass

    def monitorScenarios(self):
        self.counts["monitor"] += 1

    def cleanup(self):
        self.counts["cleanup_ran"] += 1


_orig_monitor = Scheduler.__dict__["monitorScenarios"]

mock_module = types.ModuleType("sigma.core.scheduler")
mock_module.Scheduler = Scheduler
sigma_patch._apply_scheduler_patch(mock_module)

failures = []


def check(name, cond, detail=""):
    status = "ok " if cond else "FAIL"
    print("  [%s] %s%s" % (status, name, (" - " + detail) if detail else ""))
    if not cond:
        failures.append(name)


print("sigma_patch v%s selftest (floor=%.3fs, cleanup=%.1fs)"
      % (getattr(sigma_patch, "_VERSION", "?"), FLOOR, CLEANUP_INTERVAL))

# ── 3. monitorScenarios must be untouched by default ─────────────────────────
check("monitorScenarios not wrapped",
      Scheduler.__dict__["monitorScenarios"] is _orig_monitor)
check("scanDatabaseScenarios wrapped",
      Scheduler.__dict__["scanDatabaseScenarios"] is not None and
      getattr(Scheduler.__dict__["scanDatabaseScenarios"], "__wrapped__", None)
      is not None)
check("run wrapped to record scheduler thread",
      getattr(Scheduler.__dict__["run"], "_sigma_floor_run_wrapper", False))

# ── 1. pass rate capped on the scheduler thread ──────────────────────────────
sched = Scheduler()
t = threading.Thread(target=sched.run, daemon=True)
t.start()
RUN_FOR = 0.6
time.sleep(RUN_FOR)

# ── 2. foreign-thread calls are never slept (while the loop is running) ──────
N_FOREIGN = 40
t0 = time.monotonic()
for _ in range(N_FOREIGN):
    sched.scanDatabaseScenarios()
foreign_elapsed = time.monotonic() - t0
# Un-throttled these take microseconds; with the v2 bug they would take
# >= N_FOREIGN * FLOOR = 2.0s. Allow generous slack for slow boxes.
check("foreign-thread calls not throttled", foreign_elapsed < FLOOR * N_FOREIGN / 4,
      "%d calls took %.3fs" % (N_FOREIGN, foreign_elapsed))

sched._stop = True
t.join(timeout=2.0)
check("scheduler loop thread exited", not t.is_alive())

# The loop ran ~RUN_FOR seconds with a FLOOR sleep per pass -> ~RUN_FOR/FLOOR
# passes. The foreign-thread calls above also bumped "scan", so subtract them.
loop_scans = sched.counts["scan"] - N_FOREIGN
max_passes = (RUN_FOR + 0.7) / FLOOR  # generous: timer jitter + join window
check("loop pass rate capped by floor", 0 < loop_scans <= max_passes,
      "%d passes in ~%.1fs (cap ~%d)" % (loop_scans, RUN_FOR, int(max_passes)))
check("monitorScenarios ran with the loop",
      sched.counts["monitor"] >= loop_scans)

# ── 4. cleanup gate still throttles ──────────────────────────────────────────
fresh = Scheduler()
before = fresh.counts["cleanup_ran"]
t0 = time.monotonic()
calls = 0
while time.monotonic() - t0 < CLEANUP_INTERVAL * 1.5:
    fresh.cleanup()
    calls += 1
    time.sleep(0.005)
ran = fresh.counts["cleanup_ran"] - before
check("cleanup gate active", calls > 5 and 1 <= ran <= 3,
      "%d calls -> %d actual runs" % (calls, ran))

print()
if failures:
    print("SELFTEST: FAIL (%s)" % ", ".join(failures))
    sys.exit(1)
print("SELFTEST: PASS")
sys.exit(0)
