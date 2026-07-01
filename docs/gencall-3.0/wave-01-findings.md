# Wave 1 — Bug-hunt findings & fixes

Branch: `improve/gencall-3.0-loop`
Method: 5 subsystem maps → multi-lens bug hunters → **adversarial verification**
(each candidate had to survive a skeptic trying to refute it). 16 candidates →
**12 confirmed**, 4 rejected.

## Fixed in this wave (10 — all non-call-path, tests green: 333 passed)

| # | Sev | File | Defect → Fix |
|---|-----|------|--------------|
| 2 | HIGH | `core/config.py` | `ConfigParser` BasicInterpolation crashed boot on any literal `%` in a value (pg_password, fleet_token, ssl path…). → `ConfigParser(interpolation=None)`. |
| 3 | HIGH | `core/config.py` | Un-encoded DB creds interpolated into the Postgres DSN corrupted the URL (special chars → wrong host / misdirected connection). → percent-encode user/pw/db via stdlib `quote`. |
| 4 | HIGH | `core/capture.py` | Capture watchdog could self-exit on an `is_alive()` TOCTOU, leaving a running capture with size/duration caps unenforced → unbounded pcap growth. → long-lived daemon that never self-exits + poll guarded against transient errors. |
| 5 | MED | `api/routes.py` | Fleet test launch enforced rate/call caps on **neither** controller nor worker; the direct `/api/tests/start` path could OOM the box. → `validate_caps` reject (never clamp) on the worker endpoint. |
| 6 | MED | `core/sipp_engine.py` | RTP media base-port leaked on every SIPp self-exit (finite `-m` completion or crash), shrinking the usable RTP window over uptime. → release the port in `_monitor_instance` self-exit branch + null the attr so a later `remove_instance` can't double-free a reallocated port. |
| 8 | MED | `db/migrations/__init__.py` | A re-run `ADD COLUMN` whose column already existed (ORM's `ensure_added_columns` beat it) raised "duplicate column" every boot and **permanently wedged all later migrations**. → skip an `ADD COLUMN` when the column is already present (inspector check; genuine errors still raise). |
| 9 | MED | `frontend/…/Dashboard.tsx`, `Fleet.tsx` | A persistent backend failure rendered a perpetual empty/loading view with no cause. → explicit "Controller unreachable" error state, gated on `!data` so a transient poll failure keeps last-good data. |
| 10 | LOW | `controller/aggregator.py` | `split_rate('total', …)` handed trailing nodes rate `0.0` → silent per-node 422s with no operator signal. → raise `ValueError` (too-small / non-positive total); both launch endpoints translate it to a clean `400`. |
| 11 | LOW | `core/api_gateway.py` | `RateLimiter` `deque(maxlen=1000)` silently disabled enforcement for any key whose limit exceeded 1000. → unbounded deque (the 60s sliding window already trims it). |
| 12 | LOW | `api/websocket.py` | WS handshake auth wrote a usage DB row per attempt → reconnect-storm DB hammer. → `validate_key(touch=False)` read-only path for the WS handshake. |

Regression tests: `tests/test_wave1_fixes.py` (10 new tests).

## Call-path fixes (operator-approved)

### #1 (HIGH) `core/loop_engine.py` — shaper relaunch drops `media_ip` — ✅ APPLIED (commit follows Wave 1)
**Operator signed off 2026-07-01.** Added `media_ip=old.media_ip` to the
`step_campaign_rate` relaunch so the launch UAC's media/SDP address is preserved
across every curve step. Regression test:
`tests/test_loop_shaper.py::test_step_campaign_rate_preserves_media_ip`.
⚠ Touches `-mi`/SDP — **verify on-box with a working-vs-ours pcap before deploy.**

Original finding:

### #1 (HIGH) `core/loop_engine.py` — shaper relaunch drops `media_ip`
`step_campaign_rate` (the hourly diurnal-shaper overlap-relaunch) rebuilds the
UAC **without `media_ip`**, so it falls back to the signalling IP. On a
multi-homed / public-signalling-IP box the SDP media address flips from the
on-box local interface to the signalling IP on the **first curve step** — the
exact Algeria/Chad **cause-47 one-way-audio** teardown you already fixed once,
silently reappearing. `start_campaign` (line 669) and `_restart_uac_with_csv`
(line 496) both set `media_ip` correctly; only the shaper path omits it.
**Proposed one-line fix (for your approval):** add `media_ip=old.media_ip` to the
replacement `SIPpInstance`. Changes `-mi`/SDP → **not auto-applied.**

### #7 (MED) `core/loop_engine.py` — shaper relaunch resets `-m` counter
Same function. The hourly relaunch resets SIPp's `-m` max-calls counter, so a
profiled **and** call-targeted campaign never reaches `target_calls`. Two viable
semantics (reject the combination, or carry the remaining count across
relaunches) — needs a decision on intended behaviour. Bundled here because it's
the same `step_campaign_rate` body as #1.

## Rejected by adversarial verification (4)

Candidates that a skeptic refuted by tracing an existing guard/validation — not
carried forward. (Kept out of the fix set to avoid noise; re-surfaced only if a
later wave finds new evidence.)
