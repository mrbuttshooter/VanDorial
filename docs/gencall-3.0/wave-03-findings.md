# Wave 3 — Bug-hunt findings & fixes (final hunt wave)

Branch: `improve/gencall-3.0-loop`
Method: the completeness-critic's targeted cluster — dial-pool boundary
(`pool_optimizer`), the UDP-discovery trust plane, subprocess/injection
(`capture`, `process_registry`), the migration runner, and server-side realtime —
deduped against all 18 prior findings. **4 confirmed, 2 rejected.**

**The critic declared Wave 4 NOT worthwhile** — it spot-checked the remaining
un-named seams (`loop_matcher` correlation, discovery beacon parse, capture BPF
build) and found each already defensive and dedicated-tested. Strongly
diminishing returns → **the bug hunt is complete after Wave 3.**

## Fixed in this wave (4 — all non-call-path, suite 344 → 351)

| # | Sev | File | Defect → Fix |
|---|-----|------|--------------|
| 1 | HIGH | `db/migrations/0007…sql`, `db/migrations/__init__.py` | **Postgres-only prod wedge.** `ALTER … ADD COLUMN profile_enabled BOOLEAN DEFAULT 0` — Postgres rejects an integer default on a boolean column (SQLite/CI accepts it). It aborted the whole 0007 transaction, was never recorded, **retried every boot forever**, and left `loop_campaigns` without its 8 profile columns → *every* campaign persist failed silently, so nothing was saved or resumed. → `BOOLEAN DEFAULT FALSE` in 0007 **and** a dialect rewrite (`BOOLEAN DEFAULT 0/1 → FALSE/TRUE` for Postgres) so no future migration can hit this. |
| 2 | HIGH | `controller/aggregator.py` | Fleet `completion_pct` divided `calls_in_matched` by **`calls_out`** (total attempts) instead of **`answered_out`**, contradicting the per-node `LoopMatcher` and its own docstring — understating fleet completion on every real campaign. → divide by `answered_out` (already summed) with a zero guard. |
| 3 | MED | `core/pool_optimizer.py` | `rebuild_pool_csv` resolved the origin zone by exact deck-key lookup and skipped the DB overlay, so the adaptive optimizer **permanently no-op'd** for overlay/non-exact origin zones (raised "unknown origin zone"). → resolve via `merge_zones(overlay)` + `find_zone` exactly as pool *creation* does. Only affects origin (A-number) code selection for a rebuilt pool — not dialed B-numbers or any format, so non-call-path. |
| 4 | MED | `db/models.py` | Same `BOOLEAN DEFAULT 0` in `ensure_added_columns` for `servers.dest_fixed_only` + `loop_presets.profile_enabled` (masked on fresh Postgres DBs, latent on pre-existing ones), hidden by a blanket `except: pass`. → `BOOLEAN DEFAULT FALSE` + the bare except now logs at DEBUG so a genuine ALTER failure is diagnosable. |

Regression tests: `tests/test_wave3_fixes.py` (7) + the corrected
`test_controller.py` completion-pct assertion (it had encoded the #2 bug).

## Rejected by adversarial verification (2)

Two candidates were refuted by tracing existing guards/tests (e.g. the
`loop_matcher` "shared inbound leg consumed once" behaviour is the *documented*
fix for the cy213 3× over-count, and is explicitly tested).

## Hunt complete

3 waves · 22 confirmed bugs · 8 rejected · **21 fixed & committed**, 1 call-path
item quarantined for sign-off (Wave-2 #5 adaptive-pool `-m` carry-forward). The
critic assessment: the backend is now well-covered. See `wave-01`/`wave-02`
findings for the earlier waves and `ROADMAP.md` for the GenCall 3.0 plan.
