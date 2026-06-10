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
