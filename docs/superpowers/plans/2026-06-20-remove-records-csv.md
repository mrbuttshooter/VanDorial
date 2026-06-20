# Remove records.csv Download — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Remove the per-call `records.csv` export (frontend buttons + API route + engine method + its tests). The billing system owns call records. **Live stats and CDR generation/storage stay untouched** (they are a separate path: `LoopMatcher` → `loop_stats` → WebSocket).

**Architecture:** Pure deletion across three layers + their tests. Verified safe by spec §3.2 (the export is an isolated read-only query; nothing else calls `records_csv()`).

**Tech Stack:** Python/FastAPI/SQLAlchemy backend; React/TS (Vite) frontend; pytest.

All touchpoints below were confirmed by grep on 2026-06-20.

---

## Task 1: Remove the backend endpoint, engine method, and their tests

**Files:**
- Modify: `gencall/api/loops.py`
- Modify: `gencall/core/loop_engine.py`
- Modify: `tests/test_loop_engine.py`

- [ ] **Step 1: Remove the endpoint**

In `gencall/api/loops.py`:
- Delete the entire `export_loop_records` route — the `@router.get("/api/loops/{campaign_id}/records.csv", dependencies=[Depends(require_api_key)], response_class=PlainTextResponse)` decorator and its function body (≈ lines 418–460).
- Delete the module-docstring bullet that documents it (the `GET /api/loops/{id}/records.csv export this campaign's call_records.` line, ≈ line 11).
- Remove `from fastapi.responses import PlainTextResponse` (≈ line 22) **only if** no other route in the file uses `PlainTextResponse`. Verify: `grep -n PlainTextResponse gencall/api/loops.py` returns nothing else.

- [ ] **Step 2: Remove the engine method**

In `gencall/core/loop_engine.py`, delete the `def records_csv(self, campaign_id: str) -> str:` method and its body (≈ lines 921–959). Verify nothing else calls it: `grep -rn "records_csv" gencall/` returns nothing.

- [ ] **Step 3: Remove the tests for the removed feature**

In `tests/test_loop_engine.py`, delete these tests in full (they test the now-removed export):
- `test_records_csv_export_returns_rows` (≈ 191–213)
- the local-vs-remote export test containing `client.get("/api/loops/whatever/records.csv")` (≈ 807–850)
- `test_api_records_csv` (≈ 1312–1331)
- `test_records_csv_quotes_comma_and_defangs_formula` (≈ 1439–1463)

Also update the module docstring (remove the `CSV export -> records.csv ...` bullet, ≈ line 12). Remove any import/fixture used **only** by those tests (check before deleting).

- [ ] **Step 4: Verify backend**

Run:
```
grep -rn "records_csv\|records.csv\|export_loop_records" gencall/ tests/
python -m pytest -q
```
Expected: the grep returns **no** matches in `gencall/` or `tests/`. The suite is green **except** the 2 known pre-existing failures (`test_zone_pairs_start_with_zone_codes`, `test_e164_length_by_country_and_override` — the unrelated Nigeria-Lagos E.164 issue). No import errors, no new failures.

- [ ] **Step 5: Commit**

```bash
git add gencall/api/loops.py gencall/core/loop_engine.py tests/test_loop_engine.py
git commit -m "feat(loops): remove records.csv export (billing owns records)"
```

---

## Task 2: Remove the frontend download (client + buttons + handlers)

**Files:**
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/pages/Loops.tsx`
- Modify: `frontend/src/pages/History.tsx`

- [ ] **Step 1: Remove the API client method + helper**

In `frontend/src/lib/api.ts`:
- Delete the `downloadLoopRecordsCsv:` method and its leading doc-comment (≈ 237–246).
- Delete the `downloadAuthed()` helper function (≈ 346–385). Confirmed: `downloadLoopRecordsCsv` is its only caller — verify with `grep -n downloadAuthed frontend/src/lib/api.ts` (only the definition + that one call should appear before removal).

- [ ] **Step 2: Remove the Loops page button + handler**

In `frontend/src/pages/Loops.tsx`:
- Remove the `download` handler (the `const download = async (id, box) => { … api.downloadLoopRecordsCsv … }`, ≈ 214–220).
- Remove the "Download CSV" `<Button … onClick={onDownload}>` (≈ 936–937) and the `onDownload` prop from the card component's props/type and its call sites (≈ 765–770 and the two `onDownload={() => download(...)}` usages ≈ 361/386).
- Remove `IconDownload` from the import on ≈ line 15 **only if** no longer used (`grep -n IconDownload frontend/src/pages/Loops.tsx`).

- [ ] **Step 3: Remove the History page button + handler**

In `frontend/src/pages/History.tsx`:
- Remove the `download` handler (≈ 57–63) and the "Download records CSV" `<Button>` (≈ 126–128).
- Remove the `IconDownload` import (≈ line 9) **only if** no longer used (`grep -n IconDownload frontend/src/pages/History.tsx`).

- [ ] **Step 4: Verify frontend**

Run:
```
grep -rn "downloadLoopRecordsCsv\|downloadAuthed" frontend/src
cd frontend && npm run typecheck
```
Expected: grep returns **no** matches; typecheck passes (exit 0). If `typecheck` script is absent, use `npx tsc -b --noEmit`.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/api.ts frontend/src/pages/Loops.tsx frontend/src/pages/History.tsx
git commit -m "feat(loops): remove records.csv download buttons from the UI"
```

---

## Note — prebuilt console bundle

The worker serves a prebuilt console at `gencall/web/console/assets/index-*.js`, which still contains the old code. **Do not hand-edit the minified bundle.** It regenerates from `frontend/` on the next console build/release step; flag this for the release build rather than editing artifacts.

## Self-Review

- **Spec coverage (spec §3.1):** endpoint ✓ (Task 1.1), `records_csv()` ✓ (1.2), `PlainTextResponse` import ✓ (1.1), `downloadLoopRecordsCsv` ✓ (2.1), `downloadAuthed` ✓ (2.1), Loops/History buttons + handlers + `IconDownload` ✓ (2.2/2.3). Tests for the removed feature ✓ (1.3).
- **Safety (spec §3.2):** no change to `LoopMatcher`, `loop_stats`, `call_records.py`, or the WebSocket path — live stats remain.
- **Placeholder scan:** none — each step names exact symbols + verification greps.
