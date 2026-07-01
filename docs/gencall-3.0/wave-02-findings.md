# Wave 2 — Bug-hunt findings & fixes

Branch: `improve/gencall-3.0-loop`
Method: deeper concurrency / math / CDR-integrity lenses over the modules Wave 1
touched only lightly (engine internals, persistence, control-plane, auth surface,
frontend), **deduped against the 12 Wave-1 findings**, with a completeness-critic
at the end. **6 confirmed, 0 rejected.**

## Fixed in this wave (5 — all non-call-path, suite 335 → 344)

| # | Sev | File | Defect → Fix |
|---|-----|------|--------------|
| 1 | HIGH | `core/call_records.py`, `core/config.py`, `etc/gencall.cfg` | **Billed-minute undercount.** An answered call whose hold fell between `record_max_age_s` (shipped 1800s) and the answered ceiling (7200s) was force-evicted mid-call, then re-ingested from its BYE line alone as a **0-second / code-0** record. → (a) `record_max_age_s` now defaults to and ships as `max(1800, answered_max_duration_s+300)` = 7500s, so an answerable call is never evicted while active; (b) `_persist` UPDATE now COALESCE/CASE-merges so a partial re-ingest can't null a good answered row. |
| 2 | HIGH | `core/auth_users.py` | **Deleting a console user did not revoke their live sessions** (validate() checks only token+expiry). → `delete_user` and `set_password` now purge the user's `LoginSession` rows. |
| 3 | MED | `core/stats.py` | `StatsEngine._collect` iterated `instances.values()` unlocked → a concurrent add/remove raised `RuntimeError` and dropped the entire stats snapshot. → iterate `list(...)` snapshot (mirrors the existing `sipp_engine.py` guard). |
| 4 | MED | `controller/routes.py` | **Cross-type fleet stop:** stopping a loop run via the test-stop route (or vice-versa) silently no-op'd the per-node stops but still marked the run `stopped` — leaving workers dialing. → symmetric `409` guards before any status mutation. |
| 6 | LOW | `controller/routes.py` | TOCTOU: `_is_target_online` evaluated twice per launch let a flapping node land in both/neither online+offline list. → single online-ness snapshot per node. |

Regression tests: `tests/test_wave2_fixes.py` (7) + 2 cross-type tests in
`tests/test_controller.py`.

## Quarantined — needs operator sign-off (touches the sacred call path)

### #5 (LOW) `core/loop_engine.py` — adaptive-pool relaunch resets `-m`
The same class of bug as Wave-1 #7, but on the **adaptive-pool** relaunch path
(`_restart_uac_with_csv`), not the shaper. A campaign with `target_calls>0` that
triggers an adaptive-pool UAC restart resets SIPp's `-m` counter, overshooting
its target by ~one full target per relaunch (or never terminating).

**Note:** the profiled-campaign variant is already fixed (Wave-1 #7 rejects
`profile_enabled + target_calls`). This one is trickier because **adaptive_pool
defaults ON**, so simply rejecting `target_calls` when adaptive_pool is enabled
would reject *every* call-count campaign by default. The right fix is to **carry
the already-placed count across relaunches** (`-m = max(0, target_calls −
placed_so_far)`, stop at 0) — which changes the `-m` call-generation argument, so
it's call-path and needs sign-off. **Recommended once you confirm the semantics.**

## Completeness critic — is a Wave 3 worthwhile?

Yes, but **narrowly scoped**. The critic flagged a coherent cluster the first two
waves structurally missed:

1. **`core/pool_optimizer.py`** (highest value) — un-owned by any wave AND sits at
   the sacred-call-path boundary: it rewrites the dialed number pool. Can a
   mis-classified/empty kept-set produce an **empty or malformed dial pool** that
   silently kills the loop? Needs an explicit `call_path_touch` adjudication.
2. **`core/discovery.py`** — the **second trust plane** (UDP beacons), separate
   from HTTP auth: token-compare timing safety, beacon-flood → unbounded Node
   rows, and an untrusted `address` flowing into `upsert_discovered_node` → the
   controller *connects* to it (SSRF-adjacent auto-registration).
3. **`core/capture.py`** BPF/tcpdump argv build (dest_host injection) +
   `process_registry.py` reconcile TOCTOU / DB-vs-JSON divergence.
4. **`db/migrations/__init__.py`** runner correctness — the naive `;` splitter /
   `--` comment stripper / AUTOINCREMENT rewrite, and the now-stale "audited
   0001–0005" comment while 0006/0007 exist.
5. Lower-confidence, shareable: `traffic_profile.py` sizing math, server-side
   `api_gateway`/`websocket` backpressure + listener-leak, `aggregator`/`stats`
   fan-in under partial nodes, `main.py`/`cli.py` lifespan ordering,
   `loop_matcher.py` CDR glue.

Wave 3 will target cluster 1–4 (with #5's carry-forward fix if approved).
