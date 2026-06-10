# sigma CPU/leak hotfix

A **reversible runtime patch** for the legacy ("sigma") VanDorial idle-CPU problem. It does
**not** modify the compiled `.so` files — it wraps the offending methods at import time via a
`.pth` auto-loader. Install = drop two files; rollback = delete them and restart.

> sigma is third-party (NetAxis) software. This overlay leaves the vendor binaries untouched
> and is fully reversible, but deploying it is your operational/licensing decision.

## The bug (reverse-engineered + matched to prior on-box diagnosis)

**Primary — scheduler idle-CPU busy-poll.** `sigma.core.scheduler.Scheduler.run()` runs the
log-retention cleanup (`Scheduler.cleanup` / `do_cleanup` → a burst of retention `DELETE`s)
on **every loop iteration with no throttle**, even when nothing is enabled — hundreds of
`DELETE 0` queries/sec, ~37–40 % of a core while idle. `Scheduler` is a *regular* compiled
class (no `__pyx_vtab`), so `run()`'s `self.cleanup()` dispatches through Python and a method
wrapper takes effect.

**Secondary — RTP owned-socket hygiene.** `RTPGenericStreamer` (created per dialog) self-creates
a UDP socket when `manageSocket` is truthy; its `stop()` close is guarded and swallows
failures, so an abandoned streamer can leak the owned socket/fd/port.

## The fix

1. **Scheduler:** wrap `cleanup`/`do_cleanup` with a **minimum-interval gate** — the retention
   pass runs at most once per `SIGMA_PATCH_CLEANUP_INTERVAL` seconds (default **300**). `run()`
   keeps looping, but the expensive DB pass becomes a cheap no-op between intervals → CPU drops.
2. **RTP:** wrap `stop()` to idempotently close the **owned** socket, plus a `weakref`
   finalizer for streamers GC'd without `stop()`. Externally-managed sockets are never touched.

Both are verified by `_selftest.py` against mock modules mirroring the recovered structure.

### Note: two distinct scheduler costs

The retention `DELETE` storm (every-iteration cleanup) is the **idle-CPU** culprit and is fixed
by the cleanup throttle above. Separately, **under live call load** the scheduler can busy-poll
`schedulings`/`plannings`/`testcampaigns` at hundreds of Hz (confirmed via `strace`: thousands
of `futex` + steady `poll`/`sendto`/`recvfrom`, while `pg_stat_activity` shows the DB otherwise
idle) — which also contends the GIL against call threads. If you see the scheduler thread hot
(~30%+) under load with a quiet DB, enable `SIGMA_PATCH_LOOP_FLOOR=0.1` (see controls).

### v3 — fixes the "calls going 487" regression caused by the v2 loop floor

**Symptom:** with v2's `SIGMA_PATCH_LOOP_FLOOR` enabled, test calls under load started failing
with **SIP 487 Request Terminated** (A-leg CANCELs an INVITE that wasn't answered in time).

**Cause:** v2 implemented the floor by calling `time.sleep()` inside
`monitorScenarios`/`checkPlannings`/`scanDatabase*` for **every caller**. Those methods are not
only called by the scheduler loop — scenario threads and web controllers reach them too, and
per-method sleeps compound within a pass. Sleeping there stalls live call processing
(B-side answering / scenario progress) until ring timeout → CANCEL → 487.

**v3 redesign (same env var, safe by construction):**

1. `Scheduler.run()` is wrapped to record the scheduler loop's thread id. The floor sleep fires
   **only on that thread** — calls from any other thread pass through untouched (and get logged
   once, so you can see who else calls these methods).
2. Only **one pass-entry method** carries the sleep (default `scanDatabaseScenarios`), so at
   most one floor-sleep happens per loop pass — sleeps can no longer compound.
3. `monitorScenarios` (live-call progress path) is **never wrapped by default**.
4. If `run()` cannot be wrapped, the floor is **not applied at all** (fail-safe) and a warning
   is logged.

Verified offline by `_selftest.py` (run `python _selftest.py` — it asserts the loop rate is
capped, foreign threads are never slept, and `monitorScenarios` stays untouched).

**Immediate mitigation if you're on v2 and seeing 487s right now** (before upgrading): set
`SIGMA_PATCH_LOOP_FLOOR=0` (env or `/opt/sigma/etc/sigma_patch.conf`) and restart `sigmaWeb` —
the cleanup gate (the idle-CPU fix) stays active. Then upgrade to v3 and re-enable the floor.

## Install

```bash
cd patch
chmod +x install.sh verify.py
./install.sh
# or point it at the right interpreter explicitly:
SIGMA_PYTHON=/opt/sigma/bin/python ./install.sh
```

Then restart sigma so workers load the patch:

```bash
systemctl restart sigmaWeb        # the SysV service name on this box
```

## Recommended first deploy: dry-run, then confirm CPU drop

```bash
export SIGMA_PATCH_DRY_RUN=1       # in the service environment
systemctl restart sigmaWeb
tail -f /var/log/sigma_patch.log   # or $TMPDIR/sigma_patch.log
```

Dry-run logs every decision but changes nothing. Once the log shows it would throttle
`cleanup`, drop the env var and restart again to enforce, then watch CPU:

```bash
unset SIGMA_PATCH_DRY_RUN
systemctl restart sigmaWeb
top -bH -p "$(pgrep -f sigma-web | head -1)"   # the hot scheduler thread should go quiet
```

## Verify

```bash
/opt/sigma/bin/python verify.py
```

`RESULT: OK - scheduler CPU throttle is active.` means the important fix attached. If it
reports `Scheduler ... patchable: NO (cdef extension type)`, the on-box throttle is impossible
and a recompile is required instead.

## Setting options (env var OR config file)

Every option below can be set as an **environment variable** *or* in a **config file** —
whichever is easier for your service launcher. Env vars win if both are set.

The config file is the reliable choice when the service drops privileges / doesn't propagate
env (e.g. a SysV init script). The patch reads, first match wins:
`$SIGMA_PATCH_CONF` → `/opt/sigma/etc/sigma_patch.conf` → `/etc/sigma_patch.conf`.

```bash
# enable the under-load poll-loop throttle without touching the init script:
cat > /opt/sigma/etc/sigma_patch.conf <<'EOF'
SIGMA_PATCH_LOOP_FLOOR=0.1
SIGMA_PATCH_CLEANUP_INTERVAL=300
EOF
systemctl restart sigmaWeb        # in your maintenance window
grep "loaded config" /var/log/sigma_patch.log   # confirms the file was read
```

## Controls (env vars / config-file keys)

| Var | Effect |
|---|---|
| `SIGMA_PATCH_CLEANUP_INTERVAL=N` | Min seconds between retention cleanups (default 300). |
| `SIGMA_PATCH_LOOP_FLOOR=N` | Min seconds per scheduler poll-loop pass (default 0 = off). v3: sleeps **only on the scheduler's own thread**, **once per pass** (in the method(s) below), so it can't delay call processing. Set `0.1` (≈10 Hz) if the scheduler thread stays hot (~30%+) while the DB is otherwise idle. Trade-off: pickup of *new* schedules/campaigns is delayed by up to `N` seconds (fine at 0.1). |
| `SIGMA_PATCH_FLOOR_METHODS=a,b` | Which `Scheduler` method(s) carry the floor sleep (default `scanDatabaseScenarios`, the pass entry). Do **not** add `monitorScenarios` unless you accept call-processing delays — the patch warns if you do. |
| `SIGMA_PATCH_SCHED_DISABLE=1` | Skip the scheduler half only. |
| `SIGMA_PATCH_RTP_DISABLE=1` | Skip the RTP half only. |
| `SIGMA_PATCH_DRY_RUN=1` | Log decisions, change nothing. |
| `SIGMA_PATCH_DISABLE=1` | Full no-op without uninstalling. |
| `SIGMA_PATCH_LOG=<path>` | Where events are appended (default `/var/log/sigma_patch.log`). |

## Rollback

```bash
PY=/opt/sigma/bin/python
SITE=$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
rm -f "$SITE/sigma_patch.py" "$SITE/sigma_patch.pth"
systemctl restart sigmaWeb
```

## Honest limitations

- **Built from binary RE; could not be executed against the real `.so` locally** (they're Linux
  CPython 3.11 extensions). The dry-run + `verify.py` + `top -bH` steps confirm it on the box.
- If the retention `DELETE`s turn out to be issued **inline inside `run()`** rather than via
  `self.cleanup()`/`self.do_cleanup()`, the method wrapper can't intercept them (verify.py warns
  about this). Then the only fix is a recompile / vendor build.
- The **RTP busy-spin** specifically (`queue.Empty: continue` with no sleep, *inside* `stream()`)
  is in a compiled method body and is **not** reachable by this patch. If that recurs, the known
  lever is the pure-Python `RTP.py` swap. This patch addresses the scheduler CPU cost (the real
  idle-CPU culprit) and RTP socket leakage.
