/* ============================================================================
   API contract — mirrors gencall/api/routes.py + gencall/core/stats.py +
   gencall/db/models.py. Keep in sync with the Python `to_dict()` methods.
   ============================================================================ */

export interface StatsSnapshot {
  timestamp: number;
  active_instances: number;
  total_calls: number;
  successful_calls: number;
  failed_calls: number;
  current_calls: number;
  calls_per_second: number;
  avg_response_time_ms: number;
  success_rate: number;
}

export interface InstanceStats {
  total_calls: number;
  successful_calls: number;
  failed_calls: number;
  current_calls: number;
  retransmissions: number;
  calls_per_second: number;
  avg_response_time_ms: number;
  uptime_seconds: number;
  success_rate: number;
}

export type TestState =
  | "idle"
  | "starting"
  | "running"
  | "stopping"
  | "stopped"
  | "completed"
  | "failed";

export type Transport = "udp" | "tcp" | "tls";

export interface TestInstance {
  id: string;
  scenario_file: string;
  remote_host: string;
  remote_port: number;
  local_port: number;
  local_ip: string;
  mode: string;
  transport: Transport;
  call_rate: number;
  max_calls: number;
  call_limit: number;
  duration: number;
  state: TestState;
  stats: InstanceStats;
  error_message: string;
}

export interface Scenario {
  name: string;
  path: string;
  type: "builtin" | "custom";
  description: string;
}

export interface Connector {
  id: number;
  name: string;
  description: string;
  local_ip: string;
  local_port: number;
  remote_ip: string;
  remote_port: number;
  transport: Transport;
  auth_user: string;
  enabled: boolean;
  created_at: string | null;
  updated_at: string | null;
}

export type RunStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "stopped";

export interface TestRun {
  id: number;
  name: string;
  connector_name: string;
  scenario_name: string;
  status: RunStatus;
  call_rate: number;
  max_calls: number;
  call_limit: number;
  duration: number;
  total_calls: number;
  successful_calls: number;
  failed_calls: number;
  avg_response_time_ms: number;
  error_message: string;
  started_at: string | null;
  completed_at: string | null;
  created_at: string | null;
}

export interface Health {
  status: string;
  version: string;
  name: string;
  active_tests: number;
}

/* ---- Loop campaigns (gencall/core/loop_engine.py · _public_campaign) -------
   A minutes-for-minutes loop campaign: one UAC originating from a number-pair
   CSV at a rate/concurrency, holding each call for a fixed or random duration
   until stopped or a calls/minutes target is hit. */
export type LoopStatus =
  | "running"
  | "stopped"
  | "completed"
  | "interrupted";

export type LoopDurationMode = "fixed" | "range";

export interface LoopCampaign {
  id: string;
  name: string;
  status: LoopStatus;
  node_id: number | null;
  dest_host: string;
  dest_port: number;
  transport: string;
  csv_path: string;
  rate: number;
  max_concurrent: number;
  duration_mode: LoopDurationMode;
  duration_s: number;
  duration_max_s: number;
  match_key: string;
  target_calls: number;
  target_minutes: number;
  created_at: string | null;
  started_at: string | null;
  stopped_at: string | null;
  /* Folded in by GET /api/loops/{id} (the live SIPp instance + latest match). */
  sipp?: TestInstance | null;
  loop_stats?: LoopStats | null;
}

/* One bucket of the per-call delta histogram (gencall/core/loop_matcher.py). */
export interface LoopDeltaBucket {
  ge_ms: number | null;
  lt_ms: number | null;
  count: number;
}

/* An answered outbound call whose inbound return never matched (the loop never
   closed). */
export interface LoopUnmatchedPair {
  a_number: string;
  b_number: string;
  call_uuid: string;
}

/* Per-campaign loop accounting snapshot — the WS 'loops' topic payload and the
   `loop_stats` field of GET /api/loops/{id}. Mirrors LoopMatcher.match_campaign.
   `failures.out` / `failures.in` map a SIP code string → count. */
export interface LoopStats {
  campaign_id: string;
  ts: string;
  calls_out: number;
  answered_out: number;
  minutes_out_ms: number;
  calls_in_matched: number;
  minutes_in_ms: number;
  completion_pct: number;
  delta_avg_ms: number;
  delta_p50_ms: number;
  delta_p95_ms: number;
  failures: {
    out: Record<string, number>;
    in: Record<string, number>;
  };
  delta_histogram: LoopDeltaBucket[];
  unmatched_pairs: LoopUnmatchedPair[];
}

export interface StartLoopRequest {
  name?: string;
  dest_host: string;
  dest_port?: number;
  transport?: Transport;
  /* Source IP this loop originates from ("Node = IP"). Empty => OS-routed. */
  local_ip?: string;
  csv_path?: string;
  rate?: number;
  max_concurrent?: number;
  duration_mode?: LoopDurationMode;
  duration_s?: number;
  duration_max_s?: number;
  match_key?: string;
  target_calls?: number;
  target_minutes?: number;
}

/* An origination server = a source IP a loop can run from (one loop per IP). */
export interface Server {
  id: number;
  name: string;
  ip: string;
  description: string;
  enabled: boolean;
  created_at: string | null;
}

export interface ServerRequest {
  name: string;
  ip: string;
  description?: string;
}

/* A country with its sale zones (GET /api/sale-zones), for the cascading
   Country -> Sale Zone pickers on the loop form. */
export interface SaleZoneCountry {
  name: string;
  zones: string[];
}

export interface GenerateNumbersRequest {
  origin_zone: string;
  dest_zone: string;
  origin_code?: string;
  dest_code?: string;
  count?: number;
  length?: number;
  seed?: number;
}

export interface GenerateNumbersResult {
  csv_path: string;
  count: number;
  origin_zone: string;
  dest_zone: string;
  preview: string[];
}

export interface StartTestRequest {
  name?: string;
  scenario: string;
  remote_host: string;
  remote_port?: number;
  local_ip?: string;
  local_port?: number;
  transport?: Transport;
  call_rate?: number;
  max_calls?: number;
  call_limit?: number;
  duration?: number;
  csv_file?: string;
  auth_user?: string;
  auth_pass?: string;
  extra_args?: string;
}

export interface ConnectorRequest {
  name: string;
  description?: string;
  local_ip: string;
  local_port?: number;
  remote_ip: string;
  remote_port?: number;
  transport?: Transport;
  auth_user?: string;
  auth_pass?: string;
}

/* ---- WebSocket envelope (gencall/api/websocket.py) ----------------------
   Worker topics ("stats".."test") are emitted by the single-node engine. The
   controller's /ws hub (see fleet design §4) re-uses the same envelope and adds
   fleet topics ("fleet_stats" | "node_status" | "fleet_events") so the existing
   `stream` singleton can subscribe to controller streams unchanged. */
export type StreamTopic =
  | "stats"
  | "loops"
  | "logs"
  | "test"
  | "fleet_stats"
  | "node_status"
  | "fleet_events";

export interface StreamMessage<T = unknown> {
  type: "stream" | "connected" | "subscribed" | "unsubscribed" | "error" | "pong";
  topic?: StreamTopic;
  data?: T;
  ts?: number;
}

export interface LogLine {
  ts: number;
  level: "DEBUG" | "INFO" | "WARN" | "ERROR";
  source: string;
  message: string;
}
