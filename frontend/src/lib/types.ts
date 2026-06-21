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
  local_ip?: string | null;
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
  /* GET /api/loops/fleet tags which box ran it: "local" or a worker api_url. */
  box?: string;
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

/* ---- On-demand trace (pcap) capture (gencall/core/capture.py) -------------
   One tcpdump capture for a running loop, kept on the worker until deleted.
   Mirrors CaptureManager._info(): {id, campaign_id, running, size_bytes,
   started_at, stopped_at}. `started_at`/`stopped_at` are epoch seconds. */
export interface CaptureInfo {
  id: string;
  campaign_id: string;
  running: boolean;
  size_bytes: number;
  started_at: number | null;
  stopped_at: number | null;
}

export interface StartLoopRequest {
  name?: string;
  dest_host: string;
  dest_port?: number;
  transport?: Transport;
  /* Node ("each IP one loop"): source IP + number pool come from this node. */
  node_id?: number;
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
  /* Stream real RTP media (PCMA) on each call; false = signaling-only. */
  rtp?: boolean;
  /* When rtp: loop media for the whole call; false = play once. */
  rtp_loop?: boolean;
}

/* A node = a source IP a loop runs from, carrying its own number pool (origin +
   drop sale zone). "Each IP one loop", so a node is a ready-to-run loop unit. */
export interface Server {
  id: number;
  name: string;
  ip: string;
  description: string;
  enabled: boolean;
  group_id: number | null;
  /* Remote worker this node lives on ("" = local box). */
  api_url: string;
  remote: boolean;
  has_key: boolean;
  origin_zone: string;
  dest_zone: string;
  origin_code: string;
  dest_code: string;
  pool_count: number;
  pool_length: number;
  csv_path: string;
  has_pool: boolean;
  created_at: string | null;
}

export interface ServerRequest {
  name: string;
  ip: string;
  description?: string;
  group_id?: number | null;
  /* Remote worker (one controller, many workers). Blank = local box. */
  api_url?: string;
  api_key?: string;
  origin_zone?: string;
  dest_zone?: string;
  /* Optional pinned code within each zone (e.g. only 22462). "" = whole zone. */
  origin_code?: string;
  dest_code?: string;
  count?: number;
  length?: number;
}

/* A node group = nodes sharing a destination route. Starting it fans a loop out
   to every member node (each on its own IP + pool). */
export interface NodeGroup {
  id: number;
  name: string;
  description: string;
  dest_host: string;
  dest_port: number;
  transport: string;
  rate: number;
  max_concurrent: number;
  duration_mode: LoopDurationMode;
  duration_s: number;
  duration_max_s: number;
  match_key: string;
  target_calls: number;
  target_minutes: number;
  created_at: string | null;
  /* Folded in by GET /api/node-groups. */
  nodes?: Server[];
  node_count?: number;
  running_count?: number;
}

export interface NodeGroupRequest {
  name: string;
  description?: string;
  dest_host?: string;
  dest_port?: number;
  transport?: Transport;
  rate?: number;
  max_concurrent?: number;
  duration_mode?: LoopDurationMode;
  duration_s?: number;
  duration_max_s?: number;
  match_key?: string;
  target_calls?: number;
  target_minutes?: number;
}

export interface GroupStartResult {
  status: string;
  group: string;
  started: number;
  total: number;
  results: {
    node: string;
    ip: string;
    ok: boolean;
    campaign_id?: string;
    skipped?: string;
    error?: string;
  }[];
}

export interface GeneratePoolRequest {
  origin_zone?: string;
  dest_zone?: string;
  origin_code?: string;
  dest_code?: string;
  count?: number;
  length?: number;
}

/* A saved loop "recipe" (gencall/db/models.py · LoopPreset): destination + ACD/
   rate/targets, with NO source. At run time you pick which node or group fires
   it (RunPresetRequest), so one recipe is re-runnable from anywhere. */
export interface LoopPreset {
  id: number;
  name: string;
  description: string;
  dest_host: string;
  dest_port: number;
  transport: string;
  rate: number;
  max_concurrent: number;
  duration_mode: LoopDurationMode;
  duration_s: number;
  duration_max_s: number;
  match_key: string;
  target_calls: number;
  target_minutes: number;
  /* Stream real RTP media (PCMA) on each call; false = signaling-only. */
  rtp: boolean;
  /* When rtp: loop media for the whole call; false = play once. */
  rtp_loop: boolean;
  created_at: string | null;
}

export interface LoopPresetRequest {
  name: string;
  description?: string;
  dest_host?: string;
  dest_port?: number;
  transport?: Transport;
  rate?: number;
  max_concurrent?: number;
  duration_mode?: LoopDurationMode;
  duration_s?: number;
  duration_max_s?: number;
  match_key?: string;
  target_calls?: number;
  target_minutes?: number;
  /* Stream real RTP media (PCMA) on each call; false = signaling-only. */
  rtp?: boolean;
  /* When rtp: loop media for the whole call; false = play once. */
  rtp_loop?: boolean;
}

/* Where to fire a preset: a single node, or a group (optionally a subset). */
export interface RunPresetRequest {
  node_id?: number;
  group_id?: number;
  node_ids?: number[];
}

/* ---- Traffic calculator (gencall/core/traffic_profile.py) ------------------
   The diurnal curve knobs (TrafficProfile) + the sizing result (peak/avg CPS,
   peak concurrency) returned by POST /api/loops/traffic-calc. Mirrors
   traffic_profile.make_curve kwargs + traffic_profile.calculate(). */
export interface TrafficProfile {
  preset: string;
  night_floor: number;
  ramp_up_start: number;
  plateau_start: number;
  plateau_end: number;
  ramp_down_end: number;
  tz_offset: number;
}

export interface TrafficCalcResult {
  per_hour: { hour: number; weight: number; cps: number; attempts: number }[];
  peak_cps: number;
  avg_cps: number;
  peak_concurrent: number;
  attempts_per_day: number;
  warnings: string[];
  nodes_needed: number;
}

/* A country with its sale zones (GET /api/sale-zones), for the cascading
   Country -> Sale Zone pickers on the Nodes page. */
export interface SaleZoneCountry {
  name: string;
  zones: string[];
}

/* GET /api/sale-zones payload: the country->zones tree plus a zone->codes map
   for the third "Code" dropdown (pin a single dialing code). */
export interface SaleZonesResponse {
  countries: SaleZoneCountry[];
  codes: Record<string, string[]>;
}

/* One user-added sale-zone overlay row (POST /api/sale-zones response). The
   bundled CSV deck has no id; only these DB rows are deletable. */
export interface SaleZoneRow {
  id: number;
  country: string;
  zone: string;
  code: string;
  created_at: string | null;
}

/* Body for POST /api/sale-zones — add a zone (country + label + dial code) on
   top of the bundled catalog. */
export interface SaleZoneCreate {
  country: string;
  zone: string;
  code: string;
}

/* ---- Fleet inbound trust whitelist (controller-managed) -------------------
   GET/POST /api/fleet/config/trust on the controller. The controller persists
   this singleton and fans it out to every enabled worker. Empty/disabled =
   allow-all (calls still recorded, just flagged). */
export interface FleetTrust {
  enabled: boolean;
  ips: string[];
  drop_untrusted: boolean;
}

/* POST /api/fleet/config/trust response: the saved config plus the per-node
   push outcome (one result per enabled worker). */
export interface FleetTrustResult {
  enabled: boolean;
  ips: string[];
  drop_untrusted: boolean;
  pushed?: number;
  results?: { address: string; ok: boolean; error: string | null }[];
}

/* Result of POST /api/servers/check-worker — the "Test connection" probe. */
export interface WorkerCheck {
  address: string;
  online: boolean;
  version: string | null;
  error: string | null;
}

/* One row of GET /api/fleet/resources — a node plus the live CPU/RAM of the box
   it runs on (the Fleet page). Remote nodes are polled at their api_url; numeric
   fields are null when the box is offline or the metric is unobtainable. */
export interface FleetNodeResource {
  id: number | null;
  ip: string | null;
  name: string | null;
  group_id: number | null;
  remote: boolean;
  box: string;               // "local" or the worker api_url
  online: boolean;
  error: string | null;
  hostname: string | null;
  cpu_percent: number | null;
  cores: number | null;
  load1: number | null;
  mem_total_mb: number | null;
  mem_used_mb: number | null;
  mem_percent: number | null;
}

export interface FleetResourcesResponse {
  nodes: FleetNodeResource[];
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
