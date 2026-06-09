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

## Controls (env vars)

| Var | Effect |
|---|---|
| `SIGMA_PATCH_CLEANUP_INTERVAL=N` | Min seconds between retention cleanups (default 300). |
| `SIGMA_PATCH_LOOP_FLOOR=N` | Min seconds between scheduler poll-loop iterations (default 0 = off). Throttles `monitorScenarios`/`checkPlannings`/`scanDatabase*` so `run()` can't busy-poll the DB at hundreds of Hz **under load**. Set `0.1` (≈10 Hz) if the scheduler thread stays hot (~30%+) while the DB is otherwise idle. Trade-off: scenario start/finish detection is delayed by up to `N` seconds (fine at 0.1). |
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
