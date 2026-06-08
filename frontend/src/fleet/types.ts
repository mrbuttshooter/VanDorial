/* ============================================================================
   Fleet (controller) API contract — mirrors the controller endpoints in
   docs/superpowers/specs/2026-06-08-vandorial-fleet-design.md §3–§4. Keep in
   sync with the controller's Pydantic / to_dict() shapes.

   The controller wraps a cluster of GenCall worker nodes. It reuses the worker
   StatsSnapshot/Transport shapes (see ../lib/types.ts) for aggregated and
   per-node telemetry, and adds node/group/fleet-run views on top.
   ============================================================================ */
import type { StatsSnapshot, Transport } from "../lib/types";

/* ---- Nodes (controller inventory) -------------------------------------- */
/** A single GenCall worker as seen by the controller (§3 Node, §4 NodeView). */
export interface NodeView {
  id: number;
  name: string;
  /** Base URL of the worker, e.g. "https://10.0.0.5:8080". */
  address: string;
  group_id: number | null;
  group_name: string | null;
  enabled: boolean;
  /** Derived from the last successful health probe. */
  online: boolean;
  /** ISO timestamp of the last successful contact, or null if never seen. */
  last_seen: string | null;
  /** Reported by the worker's GET /api/health when online. */
  version: string | null;
  active_tests: number;
  /** Last probe error, empty string when healthy. */
  error: string;
}

export interface CreateNodeRequest {
  name: string;
  address: string;
  group_id?: number | null;
  api_key: string;
  enabled?: boolean;
}

export interface UpdateNodeRequest {
  name?: string;
  address?: string;
  group_id?: number | null;
  api_key?: string;
  enabled?: boolean;
}

/* ---- Groups ------------------------------------------------------------- */
/** A logical group of nodes, with rollup counts (§4 GroupView). */
export interface GroupView {
  id: number;
  name: string;
  description: string;
  node_ids: number[];
  online_count: number;
  total_count: number;
}

export interface CreateGroupRequest {
  name: string;
  description?: string;
}

export interface UpdateGroupRequest {
  name?: string;
  description?: string;
  node_ids?: number[];
}

/* ---- Fleet campaigns ---------------------------------------------------- */
export type FleetRunStatus =
  | "pending"
  | "running"
  | "partial"
  | "stopped"
  | "completed"
  | "failed";

export type RateMode = "per_node" | "total";

/** Campaign destination — the SUT all targets dial (§3 FleetRun.destination). */
export interface FleetDestination {
  remote_host: string;
  remote_port?: number;
  transport?: Transport;
}

export interface FleetRate {
  mode: RateMode;
  value: number;
}

export interface FleetAuth {
  user: string;
  /** Matches the controller's AuthSpec.password (gencall/controller/routes.py). */
  password: string;
}

/** Body for POST /api/fleet/launch (§4 Fleet campaigns). */
export interface FleetLaunchRequest {
  name?: string;
  /** Target a whole group ... */
  group_id?: number | null;
  /** ... or an explicit set of nodes (one of group_id / node_ids is required). */
  node_ids?: number[];
  scenario: string;
  destination: FleetDestination;
  rate: FleetRate;
  call_limit?: number;
  max_calls?: number;
  duration?: number;
  auth?: FleetAuth;
}

/** Per-node outcome of a fan-out (§4 dispatched[] and FleetRun.results). */
export interface FleetDispatchResult {
  node_id: number;
  ok: boolean;
  test_id?: string;
  error?: string;
}

/** Response from POST /api/fleet/launch. */
export interface FleetLaunchResponse {
  fleet_run_id: number;
  dispatched: FleetDispatchResult[];
}

/** A recorded fleet campaign (§3 FleetRun, §4 FleetRunView). */
export interface FleetRunView {
  id: number;
  name: string;
  group_id: number | null;
  node_ids: number[];
  scenario: string;
  destination: FleetDestination;
  rate_mode: RateMode;
  rate_value: number;
  status: FleetRunStatus;
  started_at: string | null;
  completed_at: string | null;
  results: FleetDispatchResult[];
}

/* ---- Aggregated telemetry ---------------------------------------------- */
/** GET /api/fleet/stats — cluster-wide rollup (§4 Aggregated telemetry).
    `per_node[id]` is null when that node is offline. */
export interface FleetStats {
  /** Sum across online nodes. */
  aggregate: StatsSnapshot;
  per_group: Record<number, StatsSnapshot>;
  per_node: Record<number, StatsSnapshot | null>;
}

/* ---- WebSocket payloads (controller /ws topics, §4) -------------------- */
/** `node_status` topic — pushed when a node's reachability changes. */
export interface NodeStatusEvent {
  node_id: number;
  online: boolean;
  version: string | null;
  active_tests: number;
}

/** `fleet_events` topic — launch / stop / partial-failure notifications.
    Shape matches controller emit_fleet_event() (gencall/controller/routes.py). */
export interface FleetEvent {
  event: "launch" | "stop";
  fleet_run_id?: number;
  status?: FleetRunStatus | string;
  dispatched?: FleetDispatchResult[];
  /** Added by the WS transport envelope. */
  ts?: number;
}
