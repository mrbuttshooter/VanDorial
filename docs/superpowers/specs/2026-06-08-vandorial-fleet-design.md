# VanDorial Fleet Traffic Generator — Design

**Status:** Approved (brainstorm) · **Date:** 2026-06-08 · **Branch:** `feat/vandorial-fleet`

## 1. Product summary

VanDorial is a SIP/VoIP traffic-generation product built on the **GenCall** engine.
Today GenCall is single-node: one FastAPI server drives one local SIPp engine,
with a React "NOC console" (`frontend/`). The upgrade adds **fleet control**: one
central dashboard that orchestrates 20–30 GenCall nodes — primarily launching a
**group of nodes at a single destination** and aggregating their telemetry, with
single-node control as a secondary mode.

**Deployment target:** RHEL-family Linux VMs (Red Hat / Rocky / Alma). Container
engine: Podman or Docker. Each node runs the GenCall image; the controller runs
the same image in a different mode.

## 2. Architecture

One codebase, **two run-modes** of the same image:

- **Worker** — the existing GenCall server (REST `/api/*` + WebSocket `/ws` +
  SIPp engine), secured with `X-API-Key`. Runs on each node VM. **Unchanged in
  behavior**; only hardened.
- **Controller** — new mode (`gencall-controller` / `gencall-server --mode controller`).
  Owns the node inventory + groups, fans out commands, aggregates telemetry,
  health-checks nodes, and serves the fleet-aware console.

**The browser only ever talks to the Controller.** The controller proxies
node-scoped requests and merges live streams → one API surface, one login, and
only the controller needs network reach into nodes (firewall-friendly).

```
Browser ──REST+1×WS──▶ Controller ──REST+WS (X-API-Key)──▶ Worker × N (nodes)
```

**Resilience:** nodes keep running if the controller restarts. On reconnect the
controller reconciles by reading each node's `GET /api/tests`. Inventory/groups
live in the controller DB; no execution state is held only in memory.

## 3. Controller data model (separate DB from the worker)

- **Node**: `id, name, address (base URL e.g. https://10.0.0.5:8080), group_id?,
  api_key, enabled, created_at, last_seen, last_health (JSON: version,
  active_tests, status), online (derived)`.
- **Group**: `id, name, description, created_at`. (Node→Group is many-to-one via
  `Node.group_id`.)
- **FleetRun**: `id, name, group_id?, node_ids (JSON), scenario, destination
  (JSON: remote_host, remote_port, transport), rate_mode, rate_value, status
  (pending|running|partial|stopped|completed|failed), started_at, completed_at,
  results (JSON: per-node {node_id, ok, test_id, error})`.

API keys are stored in the controller DB. Encryption-at-rest is a Phase-3 item;
until then the DB file must be treated as a secret (already in `.gitignore`).

## 4. Controller API contract (the integration boundary)

All endpoints require `X-API-Key` (controller admin key) except `GET /api/health`.
JSON in/out. This section is the source of truth for the frontend and backend
agents — build to it exactly.

### Nodes
- `GET /api/nodes` → `{ nodes: NodeView[] }`
- `POST /api/nodes` `{ name, address, group_id?, api_key, enabled? }` → `NodeView`
- `PUT /api/nodes/{id}` `{ name?, address?, group_id?, api_key?, enabled? }` → `NodeView`
- `DELETE /api/nodes/{id}` → `{ status: "deleted", id }`
- `POST /api/nodes/{id}/check` → `NodeView` (forces an immediate health probe)

`NodeView = { id, name, address, group_id, group_name, enabled, online,
last_seen, version, active_tests, error }`

### Groups
- `GET /api/groups` → `{ groups: GroupView[] }` where
  `GroupView = { id, name, description, node_ids: number[], online_count, total_count }`
- `POST /api/groups` `{ name, description? }` → `GroupView`
- `PUT /api/groups/{id}` `{ name?, description?, node_ids? }` → `GroupView`
- `DELETE /api/groups/{id}` → `{ status: "deleted", id }`

### Fleet campaigns
- `POST /api/fleet/launch` →
  ```
  { name?, group_id?, node_ids?: number[],
    scenario, destination: { remote_host, remote_port?, transport? },
    rate: { mode: "per_node" | "total", value: number },
    call_limit?, max_calls?, duration?, auth?: { user, pass } }
  ```
  Resolves targets (group_id OR explicit node_ids), computes per-node rate, and
  fans out `POST /api/tests/start` to each online target in parallel. Returns
  `{ fleet_run_id, dispatched: [{ node_id, ok, test_id?, error? }] }`. Partial
  failures are allowed → run status `partial`.
- `POST /api/fleet/{id}/stop` → stops every member test (best-effort) → `{ status }`
- `GET /api/fleet/runs?limit=50` → `{ runs: FleetRunView[] }`
- `GET /api/fleet/runs/{id}` → `FleetRunView` with per-node results

### Aggregated telemetry
- `GET /api/fleet/stats` → `FleetStats`:
  ```
  { aggregate: StatsSnapshot,            // sum across online nodes
    per_group: { [group_id]: StatsSnapshot },
    per_node:  { [node_id]:  StatsSnapshot | null } }   // null = offline
  ```
  `StatsSnapshot` is the existing worker shape (timestamp, active_instances,
  total_calls, successful_calls, failed_calls, current_calls, calls_per_second,
  avg_response_time_ms, success_rate).
- `GET /api/fleet/stats/history?limit=240` → `{ history: StatsSnapshot[] }` (aggregate)

### Single-node passthrough
- `ALL /api/nodes/{id}/proxy/{rest_of_path}` → proxies to that node's
  `/{rest_of_path}` with the node's key injected. Lets existing console pages
  (Campaigns, Scenarios, Connectors, Console…) operate against one selected node.

### WebSocket `/ws` (controller)
Same subscribe protocol as the worker. Topics:
- `fleet_stats` — `{ aggregate, per_group, per_node }` pushed ~1 Hz.
- `node_status` — `{ node_id, online, version, active_tests }` on change.
- `fleet_events` — launch/stop/partial-failure notifications.
- `logs` — optional aggregated log lines `{ node_id, ts, level, source, message }`.

## 5. Aggregation engine

Controller keeps one connection per **enabled** node — prefer the node's WS
`stats` stream, fall back to REST `GET /api/stats` polling (default 1 s) if WS
fails. Maintains the latest snapshot per node; recomputes `aggregate` (sum) and
`per_group` rollups each tick; rebroadcasts on `fleet_stats`. A separate health
poller hits `GET /api/health` every ~5 s to set `online`/`last_seen`/`version`.

**Rate model:** `per_node` (default) → every target gets `value` cps (total =
value × online targets). `total` → controller splits `value` evenly across online
targets, distributing the remainder to the first nodes. Offline targets are
skipped and reported in `dispatched`.

## 6. Auth model

- **Browser → Controller:** `X-API-Key` (controller admin key). Reuse the worker's
  existing auth dependency/code path. Minted via `gencall keys create` (controller DB).
- **Controller → Node:** the per-node `api_key` from inventory, sent as `X-API-Key`.
- Multi-user accounts/RBAC are out of scope (future).

## 7. Console (fleet) changes

Extend the existing React app; point it at the controller.
- New top-level **scope selector** (Fleet ▸ Group ▸ Node) in the topbar.
- New pages: **Fleet Overview** (aggregate tiles + combined throughput chart +
  per-group rollup + per-node grid), **Nodes** (inventory CRUD, health, drill-in),
  **Groups** (CRUD, membership, "Launch campaign on group").
- Reuse existing pages for node scope via the proxy endpoints.
- New lib: `fleetApi.ts` + fleet types; reuse existing `components/` and `charts/`.
- Mock mode (`lib/mock.ts`) extended to simulate a fleet so the UI is demoable
  with no backend.

## 8. Phasing

- **Phase 1 — Prove & package one node (foundation).** Run GenCall + sipp end-to-end,
  fix portability/correctness gaps, finish/verify API auth (✅ done), build & test
  the container image (Podman/Docker on RHEL), first git commit (✅), integration
  test (boot → health → start → stats). *Deliverable: tested image + console + CI.*
- **Phase 2 — Fleet control plane.** Controller (inventory, groups, fan-out,
  aggregation, health, proxy, WS hub) + fleet console + fleet mock. Validate with
  2–3 nodes. *Deliverable: working controller image + fleet dashboard.*
- **Phase 3 — Scale & harden.** Provisioning to stand up N workers, secrets
  encryption + TLS, monitoring, scale-test to 30 nodes, capacity tuning.

## 9. Non-goals (YAGNI)

Multi-tenant/RBAC user accounts; orchestration auto-discovery (designed pluggable,
not built); cross-node call correlation; editing live SIPp scenarios mid-run.

## 10. Environment note for implementers

This dev sandbox is Windows without `sipp`. Frontend is fully verifiable
(`tsc`/`vitest`/`vite build`). Python is import/compile + unit-test verifiable
(`pip` deps installed; `tests/` run without sipp via mocks). **Live SIP traffic
and 30-node scale validation must happen on a RHEL box with sipp** — these steps
are captured as a runbook, not executed here.
