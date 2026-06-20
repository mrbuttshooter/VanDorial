# GenCall — Sale Zones, Remove records.csv, On-Demand Trace, Controller-Managed Whitelist

**Date:** 2026-06-20
**Status:** Approved design (pre-implementation)
**Scope:** Four changes in one plan. They are independent and can be implemented/sequenced separately.

---

## 1. Background & Goals

GenCall already generates A/B test numbers in-app from a country → sale-zone → dial-code catalog (no CSV upload). Four changes are wanted:

1. **Editable sale-zone catalog** — today it is a read-only bundled CSV. Add the ability to create new sale zones (country + zone label + dial code) from the app.
2. **Remove the `records.csv` download** — the per-call CDR export is redundant; the user has a separate billing system that owns call records.
3. **On-demand trace (pcap) capture** — capture a running loop's packets with `tcpdump` on the worker, keep the file on the worker, and pull it to the controller only when explicitly requested.
4. **Controller-managed trust whitelist** — remove the install-time whitelist prompt; let the client enable/set the inbound trust whitelist from the controller and push it to all workers when needed.

Non-goals are listed in §8.

---

## 2. Part 1 — Editable Sale Zones

### 2.1 Current state

- Catalog is a bundled, read-only CSV: `gencall/scripts/data/sale_codes.csv` (and `sale_codes.sample.csv`), format `zone,code`. A zone may have multiple codes.
- Country is **derived** from the zone name (text before the first `-`): e.g. `Nigeria-Lagos` → `Nigeria` (`gencall/scripts/gen_loop_csv.py`).
- Served by `GET /api/sale-zones` (`gencall/api/loops.py:942`), returning `{ countries: [{name, zones[]}], codes: {zone: [codes]} }`, built via `gen_loop_csv.build_country_tree()`. Cached in `_ZONES_CACHE` (`gencall/api/loops.py:920`).
- E.164 number length is determined by `E164_TOTAL_LEN` (hardcoded longest-prefix-match table, `gen_loop_csv.py:165`) with a default-length fallback. **Unchanged** — new zones use the same length resolution as existing zones ("E.164 as the others work").
- There is **no write path**; the catalog is immutable.

### 2.2 Design — overlay model

Keep the bundled CSV as the immutable **base**; add user-created zones as a DB **overlay** merged on top. Add-only.

**New DB table `SaleZone`** (`gencall/db/models.py`):

| column | type | notes |
|---|---|---|
| `id` | int PK | |
| `country` | str | stored explicitly (not derived) for robust grouping |
| `zone` | str | full zone label, e.g. `Algeria-Mobile (Djezzy)` |
| `code` | str | one dial code; multiple codes for a zone = multiple rows |
| `created_at` | datetime | |

Unique constraint on `(zone, code)`. **No `total_len` column** — length comes from existing `E164_TOTAL_LEN` logic, identical to every other zone.

**Catalog assembly:** `_zones()` / `build_country_tree()` merge CSV-base rows with `SaleZone` rows into the same `{zone: [codes]}` map and country tree. DB rows group under their explicit `country`; CSV rows keep their derived country; case-insensitive folding lands an added zone under an existing country. Cache invalidated on any write.

### 2.3 API

- `POST /api/sale-zones` — body `{ country, zone, code }`. Inserts a row, invalidates cache, returns updated catalog (or new row). Validates non-empty fields and digit-only code.
- `DELETE /api/sale-zones/{id}` — removes a **user-added** row only (bundled CSV entries have no id and are not deletable). Invalidates cache.
- `GET /api/sale-zones` — unchanged shape; now reflects base + overlay.

### 2.4 Generation flow

A newly-added zone flows into `generate_pairs()` (`gen_loop_csv.py:376`) automatically via the merged `{zone: [codes]}` map. Its dial code passes through the existing `e164_total_len()` longest-prefix lookup (default-length fallback) — identical handling to existing zones.

### 2.5 Frontend

On the Nodes page (`frontend/src/pages/Nodes.tsx`, consumes `api.saleZones()` ~line 62, cascade ~lines 76–82):

- Add a **"+ Add sale zone"** control opening a small modal: **Country**, **Zone label**, **Dial code**.
- On save → `POST /api/sale-zones` → refetch `saleZones()` so the new zone is immediately selectable.
- Optionally list user-added zones with a delete affordance (`DELETE /api/sale-zones/{id}`).

### 2.6 Assumption to verify during planning

The fleet shares **one Postgres**, so a zone added on the controller is visible to workers that generate numbers. If worker DBs are separate, the controller remains catalog authority and passes resolved generation params (zone/code) to workers.

---

## 3. Part 2 — Remove the records.csv Download

### 3.1 Touchpoints to remove

**Backend:**
- `GET /api/loops/{campaign_id}/records.csv` endpoint (`gencall/api/loops.py:418`).
- `loop_engine.records_csv()` (`gencall/core/loop_engine.py:921`) — delete; verify no other caller.
- Drop the now-unused `PlainTextResponse` import in `loops.py` if no other route uses it.

**Frontend:**
- `downloadLoopRecordsCsv()` (`frontend/src/lib/api.ts:241`).
- `downloadAuthed()` helper (`frontend/src/lib/api.ts:350`) — remove **only if** no other download uses it (records.csv appears to be its sole caller; confirm).
- "Download CSV" button + handler on Loops (`frontend/src/pages/Loops.tsx`: button ~937, handler ~214, `onDownload` plumbing ~765/936).
- "Download records CSV" button + handler on History (`frontend/src/pages/History.tsx`: button ~126, handler ~57).
- Remove `IconDownload` import in each file if unused afterward.

### 3.2 Safety (verified during exploration)

Live "eyeball" stats (answered / failed / call_rate / completion) are produced by a **separate path**: `LoopMatcher` → `loop_stats` table → WebSocket "loops" topic (`gencall/api/websocket.py`), consumed by the Loops page. The `records.csv` export is an isolated read-only query. **Removing the export does not affect** call-record generation, the tail parser (`call_records.py`), `loop_stats`, or any live monitoring. CDR generation/storage is left untouched.

**Decision — live info, no file:** the live call-info / measurement UI (minutes, completion %, answered/failed) is **retained** — it is fed by the live `loop_stats` path and still works without the CSV. This matches the user's intent: *see the calls' information live, then it's done, with no file to keep or manage.* Only the downloadable CSV artifact is removed. (Removing the CSV does not empty the live view, so by the rule "if it still gives info, keep it," the view stays. The History page of past runs is also left intact unless decided otherwise.)

---

## 4. Part 3 — On-Demand Trace (pcap) Capture

### 4.1 Current state

No packet-capture feature exists in GenCall (confirmed; only obsolete `play_pcap_audio` comments remain). The colleague's tool has a single-box `tcpdump` start/stop/download (`/api/trace/*`). We adapt that to the multi-node fleet.

### 4.2 Behavior (as steered by the user)

- **On-demand**, started/stopped manually **while a loop is running** — never automatic.
- The pcap **stays on the worker** by default (a trace you don't want just sits there or gets deleted).
- It is **pulled to the controller only when explicitly requested** (download). "Keep it at that worker's API."

### 4.3 Worker side

The worker (runs SIPp + the loop) gains a small **capture manager** + endpoints:

- `POST /api/loops/{campaign_id}/capture/start` → launch `tcpdump` for that loop, writing a pcap to a worker scratch dir; returns a capture id. **Filter:** scoped to the loop's destination switch IP (both directions) — captures the loop's SIP signalling + RTP media out (UAC) and back (UAS), mirroring the colleague's `host <ip> and udp`. The campaign's allocated SIP/media ports (from its `SIPpInstance`) and `remote_host` are available to build the filter.
- `POST /api/loops/{campaign_id}/capture/stop` → stop that loop's capture.
- `GET /api/captures` → list captures on this worker (capture id, campaign id, size, started/stopped, running?).
- `GET /api/captures/{cap_id}` → stream the pcap bytes (**the pull**).
- `DELETE /api/captures/{cap_id}` → delete a capture file.

**Guardrails:** snaplen + a size cap and/or max-duration auto-stop so a long capture can't fill the disk. Captures persist after stop until explicitly deleted.

### 4.4 Controller side

Thin routes that locate the loop's worker and proxy these calls via `NodeClient`. The generic `proxy()` (`gencall/controller/node_client.py:134`) streams the worker response back verbatim — ideal for the download. The pull happens **only on explicit user request**; nothing is auto-shipped.

### 4.5 Frontend

On a running loop: **Start / Stop Capture** controls; a **Captures** list with **Download** (pull) and **Delete**.

### 4.6 Privileges & dependency (honest flags)

- `tcpdump` must be present on each worker (the colleague's installer apt-installs it; note as an install requirement).
- `tcpdump` needs root or `cap_net_raw` — document the `setcap`/run-as step. If unavailable, `capture/start` returns a clear error rather than failing silently.

---

## 5. Part 4 — Controller-Managed Trust Whitelist (remove install prompt)

### 5.1 Current state

- Two distinct whitelists:
  - **App-layer trust whitelist** — `[trust] whitelist` + `drop_untrusted` in `gencall.cfg` (`config.py:369`/`:382`). *Verification-only*: an inbound call whose source IP isn't listed is **flagged untrusted** (default) or **dropped** (`drop_untrusted=true`). Applied per inbound record in `call_records.py:_apply_trust_filter` (~410) via `ip_in_whitelist` (~47). **Empty list = allow-all** (fresh install isn't broken).
  - **Host firewall** — nftables/ufw rules (`docs/deploy/loop-runner.md §2`) restricting UDP/5060 + RTP to the MADA IPs. The code calls this the **REAL trust boundary**. It is a **manual** ops step (deliberately not automated, to avoid lockout).
- **Install-time prompt (to remove):** `deploy/install.sh:95-115` and `deploy/install-ubuntu.sh:152-157` interactively prompt for the MADA whitelist and write it to `[trust] whitelist` (with an `MADA_IPS` env override).
- The app-layer whitelist is **read once at startup** (`main.py:201`, passed to `CallRecordParser`); **no runtime reload, no UI, no API**.
- **Controller→worker config push does NOT exist** — `NodeClient` only does health/stats/start_loop/stop_loop/proxy. The fan-out pattern (NodeClient + `X-API-Key`, used by node-groups) is reusable.

### 5.2 Scope decision (app-layer whitelist only)

This change centralizes the **app-layer `[trust] whitelist`** (the verification/visibility layer). The **host firewall stays a manual ops step and remains the real boundary** — the controller does **not** manage firewall rules (auto-changing a worker's firewall risks locking the box out). See §8.

### 5.3 Design

**Remove from install:**
- Delete the whitelist prompt + `set_cfg trust whitelist` (and `MADA_IPS` handling) from `deploy/install.sh` and `deploy/install-ubuntu.sh`. Fresh installs default to empty = allow-all-but-flag (unchanged default semantics).
- Update `docs/deploy/loop-runner.md`: the app-layer whitelist is now set from the controller UI; the **firewall section stays** (still the real boundary, still manual).

**Worker — runtime trust config (new):**
- `POST /api/config/trust/whitelist` — body `{ enabled: bool, ips: [string], drop_untrusted: bool }`. **Hot-updates** the running `CallRecordParser`'s whitelist + drop flag via a **thread-safe setter** (the parser reads the current value per record) — no restart. `enabled=false` (or empty `ips`) = allow-all-but-flag.
- `GET /api/config/trust/whitelist` — return the worker's current effective trust config.
- Auth via existing `X-API-Key`.

**Controller — source of truth + fan-out (new):**
- Store the fleet trust-whitelist config in the controller DB (single fleet-wide setting: `enabled`, `ips`, `drop_untrusted`).
- `GET /api/fleet/trust/whitelist` / `POST /api/fleet/trust/whitelist` — read/set the setting, then **fan out** to all online workers via a new `NodeClient.set_trust_whitelist()` (mirrors the node-group start fan-out), returning per-worker apply status.
- **Re-push on worker (re)join/reconnect** so a new or restarted worker receives the current whitelist (else it would run allow-all until the next change).
- **Single-box mode:** the controller endpoint applies locally; "all other workers (if needed)" is a no-op when there are none.

**Frontend (new):**
- A controller Settings panel: toggle **Enable whitelist**, edit the **IP/CIDR list**, choose **flag vs drop**, and **Apply** (pushes to all workers). Show per-worker apply status and the current effective value.

### 5.4 Persistence/sync decision (for planning)

Controller DB is the **single source of truth**; pushed on change and on worker join. Workers hold the value in memory. Whether a worker also writes-through to its local `gencall.cfg` (to survive a standalone restart without the controller) vs always re-pulls from the controller on start is a planning detail — recommended default: **controller-as-source-of-truth + re-pull on join** (simpler, consistent across the fleet).

### 5.5 Note

This introduces a small, reusable **controller→worker config-push** mechanism (endpoint pair + `NodeClient` method + thread-safe runtime apply). Trust whitelist is its first user; other runtime settings could ride the same rail later (out of scope now).

---

## 6. Data Flow Summary

- **Add zone:** UI modal → `POST /api/sale-zones` → `SaleZone` row + cache invalidation → `GET /api/sale-zones` returns base+overlay → cascade refreshes → number generation uses it via merged map + existing E.164 logic.
- **Remove export:** delete backend route + helper + frontend buttons/handlers → no remaining caller of `records_csv()` → live stats path unchanged.
- **Trace:** UI Start → controller proxies → worker `tcpdump` writes pcap (stays on worker) → user clicks Download → controller proxies/streams the worker file to the browser → optional Delete.
- **Whitelist:** UI Apply → `POST /api/fleet/trust/whitelist` → controller persists → fan-out `NodeClient.set_trust_whitelist()` to all workers → each worker hot-updates its `CallRecordParser` → per-worker status returned; controller re-pushes to any worker that (re)joins.

---

## 7. Testing

**Part 1 (sale zones):**
- Unit: merge of CSV base + `SaleZone` overlay yields expected `{zone: [codes]}` map and country tree; case-insensitive country folding; cache invalidation on write.
- Unit: a newly-added zone/code generates correct-length numbers via existing E.164 logic (default-length fallback when the code isn't in the table — matching existing behavior).
- API: `POST` validation (empty fields, non-digit code, duplicate `(zone, code)`); `DELETE` removes only overlay rows; `GET` reflects additions.

**Part 2 (remove records.csv):**
- Backend: `GET /api/loops/{id}/records.csv` returns 404; no import errors.
- Frontend: build passes with no dangling references; Loops & History render without the button.
- Regression: live stats still stream and render (separate path intact).

**Part 3 (trace):**
- Worker: start/stop lifecycle; filter correctness; size-cap / max-duration auto-stop; list/delete.
- Controller: download proxies bytes through to the client; capture stays on worker until pulled/deleted.
- Errors: `tcpdump` missing / no privilege; worker offline; capture on a non-running loop (→ 409).

**Part 4 (whitelist):**
- Worker: `POST /api/config/trust/whitelist` hot-updates the filter with no restart; inbound from a listed IP → trusted; from an unlisted IP → flagged (default) or dropped (`drop_untrusted`); `enabled=false`/empty → allow-all. Thread-safety under the tail-poll loop.
- Controller: `POST /api/fleet/trust/whitelist` persists + fans out; per-worker apply status; re-push on a (re)joining worker; single-box applies locally.
- Install: deploy scripts no longer prompt for / write the whitelist; fresh install defaults to allow-all-but-flag.

---

## 8. Out of Scope

- **CDR / minute-accounting changes** — billing owns records; `LoopMatcher`, `call_records`, retention, and live stats untouched.
- **Editing or deleting bundled (CSV) zones** — overlay is add-only; deletion applies to user-added rows only.
- **Per-zone manual number length** — excluded per the user; existing E.164 logic applies uniformly.
- **Automatic / always-on capture** — trace is strictly on-demand.
- **Controller-managed host firewall** — nftables/ufw stays a manual ops step and the real boundary; the controller manages only the app-layer trust whitelist. Auto-managing firewalls (lockout risk) is explicitly out of scope.

---

## 9. Open Items to Confirm in Planning

1. Fleet shares one Postgres (§2.6).
2. `downloadAuthed()` has no other caller (§3.1).
3. `SaleZone` starts empty (overlay); CSV provides the base — no seed/migration of existing zones needed.
4. `tcpdump` availability + privilege model on workers (§4.6); capture retention/disk-usage cap; capture↔loop keying on a worker running multiple loops.
5. Whitelist scope is app-layer only, firewall stays manual (§5.2) — confirm.
6. Whitelist persistence: controller-as-source-of-truth + re-pull on join vs worker write-through to cfg (§5.4).
7. Worker runtime config reload must be thread-safe vs the `CallRecordParser` tail-poll thread (§5.3).
