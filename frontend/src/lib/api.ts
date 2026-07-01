/* ============================================================================
   Typed REST client for the GenCall API (gencall/api/routes.py).
   All calls go through one `request()` so error handling + the mock fallback
   live in a single place.
   ============================================================================ */
import type {
  CaptureInfo,
  Connector,
  ConnectorRequest,
  FleetResourcesResponse,
  FleetTrust,
  FleetTrustResult,
  GeneratePoolRequest,
  GroupStartResult,
  Health,
  LoginResult,
  LoopCampaign,
  LoopPreset,
  LoopPresetRequest,
  MeResult,
  NodeGroup,
  NodeGroupRequest,
  RunPresetRequest,
  SaleZoneCreate,
  SaleZoneRow,
  SaleZonesResponse,
  Scenario,
  Server,
  ServerRequest,
  StartLoopRequest,
  StartTestRequest,
  StatsSnapshot,
  TestInstance,
  TestRun,
  TrafficCalcResult,
  TrafficProfile,
  WorkerCheck,
} from "./types";
import { mockApi, MOCK_ENABLED } from "./mock";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

const BASE = ""; // same origin; Vite proxies /api in dev

/* ---- API key -------------------------------------------------------------
   The backend (gencall/api/routes.py) enforces an `X-API-Key` header on every
   endpoint except /api/health. We keep the key in localStorage and attach it
   to every request. Use setApiKey() from a settings screen to configure it. */
const API_KEY_STORAGE = "gencall_api_key";

export function getApiKey(): string | null {
  try {
    return localStorage.getItem(API_KEY_STORAGE);
  } catch {
    return null;
  }
}

export function setApiKey(key: string | null): void {
  try {
    if (key) localStorage.setItem(API_KEY_STORAGE, key);
    else localStorage.removeItem(API_KEY_STORAGE);
  } catch {
    /* storage unavailable (e.g. private mode) — ignore */
  }
}

/* ---- Auth-required signal ------------------------------------------------
   When request() hits an unrecoverable 401 it clears the stored token and
   fires this so the root can drop back to the Login page without a reload.
   A plain callback set keeps it dependency-free and idiomatic for this app. */
type AuthListener = () => void;
const authListeners = new Set<AuthListener>();

export function onAuthRequired(fn: AuthListener): () => void {
  authListeners.add(fn);
  return () => authListeners.delete(fn);
}

function signalAuthRequired(): void {
  setApiKey(null);
  for (const fn of authListeners) fn();
}

/** Explicitly drop to the Login page (e.g. after the operator logs out). */
export function requireAuth(): void {
  signalAuthRequired();
}

/* Console auto-auth: ask the controller for the console's API key at startup so
   ANY browser that opens /console is authenticated — the key no longer has to
   be pasted into each browser by hand. The backend serves it from
   /api/console/bootstrap only when it serves the console; a 404 (fleet worker /
   external-API box) just leaves any manually-set key in place. Always run this
   before the first data fetch (see main.tsx). Skipped in mock mode. */
export async function bootstrapApiKey(): Promise<void> {
  if (MOCK_ENABLED) return;
  try {
    const res = await fetch(BASE + "/api/console/bootstrap");
    if (!res.ok) return; // 404 => no auto-auth on this box; keep existing key
    const body = (await res.json()) as { api_key?: string };
    if (body.api_key) setApiKey(body.api_key);
  } catch {
    /* backend unreachable — fall back to whatever key is stored */
  }
}

async function request<T>(
  path: string,
  init?: Omit<RequestInit, "body"> & { body?: unknown },
): Promise<T> {
  const headers: Record<string, string> = { ...(init?.headers as Record<string, string>) };
  const apiKey = getApiKey();
  if (apiKey) headers["X-API-Key"] = apiKey;

  const opts: RequestInit = { ...init, body: undefined, headers };
  if (init?.body !== undefined) {
    opts.body = JSON.stringify(init.body);
    headers["Content-Type"] = "application/json";
  }

  let res: Response;
  try {
    res = await fetch(BASE + path, opts);
  } catch (networkErr) {
    // Backend unreachable. In mock mode we never get here (intercepted below),
    // so surface a clear, actionable error.
    throw new ApiError(0, `Network unreachable: ${String(networkErr)}`);
  }

  // A 401 from a normal data call means the stored token is gone/expired. The
  // backend now requires a real login (bootstrap may 404 once a user exists), so
  // we no longer loop on bootstrap here — that would mask the need to log in.
  // Clear the token and surface the Login page via the auth signal. The auth
  // endpoints (/api/auth/*) handle their own 401s and must NOT trip this, or the
  // startup probe + login form would clear themselves mid-flight.
  if (res.status === 401 && !path.startsWith("/api/auth/")) {
    signalAuthRequired();
  }

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? body.error ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/* Authenticated file download: the X-API-Key header can't ride on a plain
   <a href> or window.open, so fetch the bytes with the key, wrap them in a
   blob, and click a synthetic anchor to save them with the given filename. */
async function downloadAuthed(path: string, filename: string): Promise<void> {
  const headers: Record<string, string> = {};
  const k = getApiKey();
  if (k) headers["X-API-Key"] = k;
  const res = await fetch(BASE + path, { headers });
  if (!res.ok) throw new ApiError(res.status, res.statusText);
  const url = URL.createObjectURL(await res.blob());
  try {
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
  } finally {
    URL.revokeObjectURL(url);
  }
}

/* When MOCK_ENABLED, route through the in-browser mock so the console is fully
   demoable without a running backend. Real calls otherwise. */
function call<T>(real: () => Promise<T>, mock: () => Promise<T>): Promise<T> {
  return MOCK_ENABLED ? mock() : real();
}

/* ---- Loop-campaign demo data (mock mode only) --------------------------
   A live fleet loop running across several nodes plus saved presets, so the
   Loops page is fully demoable without a backend. */
const LOOP_PRESETS: LoopPreset[] = [
  { id: 1, name: "EU-Retail-Diurnal", description: "Retail A→B, diurnal shaping + PCMA media", dest_host: "sbc-eu.carrier.net", dest_port: 5060, transport: "udp", rate: 60, max_concurrent: 1200, duration_mode: "fixed", duration_s: 114, duration_max_s: 0, match_key: "exact", target_calls: 0, target_minutes: 240000, rtp: true, rtp_loop: true, profile_enabled: true, profile_preset: "diurnal", night_floor: 0.25, ramp_up_start: 6, plateau_start: 9, plateau_end: 20, ramp_down_end: 23, tz_offset: 0, created_at: "2026-06-28T09:12:00Z" },
  { id: 2, name: "Carrier-Soak-1k", description: "Sustained 1000-concurrent soak on the carrier trunk", dest_host: "trunk.lon.carrier.net", dest_port: 5060, transport: "tcp", rate: 90, max_concurrent: 1000, duration_mode: "fixed", duration_s: 180, duration_max_s: 0, match_key: "exact", target_calls: 0, target_minutes: 0, rtp: false, rtp_loop: false, profile_enabled: false, profile_preset: "diurnal", night_floor: 0.25, ramp_up_start: 6, plateau_start: 9, plateau_end: 20, ramp_down_end: 23, tz_offset: 0, created_at: "2026-06-27T14:03:00Z" },
  { id: 3, name: "IMS-Register-Soak", description: "Registration soak against the IMS core (TLS)", dest_host: "ims.fra.core.net", dest_port: 5061, transport: "tls", rate: 40, max_concurrent: 600, duration_mode: "range", duration_s: 60, duration_max_s: 300, match_key: "prefix", target_calls: 0, target_minutes: 120000, rtp: false, rtp_loop: false, profile_enabled: true, profile_preset: "diurnal", night_floor: 0.3, ramp_up_start: 7, plateau_start: 10, plateau_end: 19, ramp_down_end: 22, tz_offset: 0, created_at: "2026-06-25T08:40:00Z" },
];

function loopCampaign(preset: LoopPreset, seq: number, ip: string, node_id: number, callsOut: number, live: number): LoopCampaign {
  const answered = Math.round(callsOut * 0.94);
  const matched = Math.round(answered * 0.98);
  return {
    id: `cmp-${preset.id}-${seq}`,
    name: `${preset.name}-${ip}`,
    status: "running",
    node_id,
    local_ip: ip,
    dest_host: preset.dest_host,
    dest_port: preset.dest_port,
    transport: preset.transport,
    csv_path: `pools/${ip}.csv`,
    rate: preset.rate,
    max_concurrent: preset.max_concurrent,
    duration_mode: preset.duration_mode,
    duration_s: preset.duration_s,
    duration_max_s: preset.duration_max_s,
    match_key: preset.match_key,
    target_calls: preset.target_calls,
    target_minutes: preset.target_minutes,
    created_at: preset.created_at,
    started_at: "2026-07-01T06:05:00Z",
    stopped_at: null,
    box: node_id <= 3 ? "local" : `https://${ip}:8080`,
    loop_stats: {
      campaign_id: `cmp-${preset.id}-${seq}`,
      ts: "2026-07-01T06:41:00Z",
      calls_out: callsOut,
      answered_out: answered,
      minutes_out_ms: answered * 114000,
      minutes_out_today_ms: answered * 114000,
      calls_in_matched: matched,
      minutes_in_ms: matched * 113200,
      completion_pct: preset.target_minutes ? Math.min(99, Math.round((answered * 1900) / preset.target_minutes)) : 0,
      delta_avg_ms: 42, delta_p50_ms: 38, delta_p95_ms: 88,
      failures: { out: { "486": Math.round(callsOut * 0.03), "408": Math.round(callsOut * 0.01) }, in: {} },
      delta_histogram: [
        { ge_ms: null, lt_ms: 20, count: Math.round(matched * 0.15) },
        { ge_ms: 20, lt_ms: 40, count: Math.round(matched * 0.45) },
        { ge_ms: 40, lt_ms: 80, count: Math.round(matched * 0.30) },
        { ge_ms: 80, lt_ms: null, count: Math.round(matched * 0.10) },
      ],
      unmatched_pairs: [],
    },
  };
}

const LOOP_CAMPAIGNS: LoopCampaign[] = [
  loopCampaign(LOOP_PRESETS[0], 1, "10.20.1.11", 1, 128400, 1150),
  loopCampaign(LOOP_PRESETS[0], 2, "10.20.2.21", 5, 131200, 1180),
  loopCampaign(LOOP_PRESETS[0], 3, "10.20.3.31", 9, 129900, 1165),
  loopCampaign(LOOP_PRESETS[1], 1, "10.20.2.22", 6, 96700, 980),
  loopCampaign(LOOP_PRESETS[1], 2, "10.20.2.23", 7, 95300, 972),
];

/* ---- Nodes / number pools / groups demo data (mock mode only) ---------- */
function server(id: number, name: string, ip: string, group_id: number | null, remote: boolean, oz: string, oc: string, dz: string, dc: string, pool: number): Server {
  return {
    id, name, ip, description: `${remote ? "remote worker" : "local"} source node`,
    enabled: true, group_id, api_url: remote ? `https://${ip}:8080` : "", remote,
    has_key: remote, origin_zone: oz, dest_zone: dz, origin_code: oc, dest_code: dc,
    dest_fixed_only: false, pool_count: pool, pool_length: pool,
    csv_path: `pools/${ip}.csv`, has_pool: pool > 0, created_at: "2026-06-20T10:00:00Z",
  };
}
const SERVERS: Server[] = [
  server(1, "ny-gen-01", "10.20.1.11", 1, false, "France", "+33", "France Mobile", "+336", 5000),
  server(2, "ny-gen-02", "10.20.1.12", 1, false, "France", "+33", "France Fixed", "+331", 5000),
  server(3, "ny-gen-03", "10.20.1.13", 1, false, "Belgium", "+32", "Belgium Mobile", "+3247", 4000),
  server(5, "lon-gen-01", "10.20.2.21", 2, true, "United Kingdom", "+44", "UK Mobile", "+447", 8000),
  server(6, "lon-gen-02", "10.20.2.22", 2, true, "United Kingdom", "+44", "UK Fixed", "+4420", 8000),
  server(9, "fra-gen-01", "10.20.3.31", 3, true, "Germany", "+49", "Germany Mobile", "+4915", 6000),
  server(11, "fra-gen-03", "10.20.3.33", 3, true, "Algeria", "+213", "Algeria Mobile", "+2135", 6000),
];
const SALE_ZONES: SaleZonesResponse = {
  countries: [
    { name: "France", zones: ["France Fixed", "France Mobile"] },
    { name: "United Kingdom", zones: ["UK Fixed", "UK Mobile"] },
    { name: "Germany", zones: ["Germany Fixed", "Germany Mobile"] },
    { name: "Belgium", zones: ["Belgium Fixed", "Belgium Mobile"] },
    { name: "Algeria", zones: ["Algeria Fixed", "Algeria Mobile"] },
  ],
  codes: {
    "France Mobile": ["+336", "+337"], "France Fixed": ["+331", "+334"],
    "UK Mobile": ["+447"], "UK Fixed": ["+4420"], "Germany Mobile": ["+4915"],
    "Belgium Mobile": ["+3247"], "Algeria Mobile": ["+2135", "+2136"],
  },
};
function nodeGroup(id: number, name: string, desc: string, dest_host: string, transport: string, rate: number, mc: number): NodeGroup {
  const members = SERVERS.filter((s) => s.group_id === id);
  return {
    id, name, description: desc, dest_host, dest_port: transport === "tls" ? 5061 : 5060,
    transport, rate, max_concurrent: mc, duration_mode: "fixed", duration_s: 114, duration_max_s: 0,
    match_key: "exact", target_calls: 0, target_minutes: 0, created_at: "2026-06-20T10:00:00Z",
    nodes: members, node_count: members.length, running_count: members.length,
  };
}
const NODE_GROUPS: NodeGroup[] = [
  nodeGroup(1, "EU-retail", "European retail A→B nodes", "sbc-eu.carrier.net", "udp", 60, 1200),
  nodeGroup(2, "LON-carrier", "London carrier-trunk load nodes", "trunk.lon.carrier.net", "tcp", 90, 1000),
  nodeGroup(3, "FRA-ims", "Frankfurt IMS / registration soak nodes", "ims.fra.core.net", "tls", 40, 600),
];

export const api = {
  // ---- Auth (gencall/api/auth.py) ----
  // The returned token IS the API key, so login stores it under the same slot
  // every other call reads from. In mock mode there is no backend, so accept
  // any credentials and stash a dummy token to keep the demo working offline.
  login: (username: string, password: string) =>
    call<LoginResult>(
      async () => {
        const res = await request<LoginResult>("/api/auth/login", {
          method: "POST",
          body: { username, password },
        });
        setApiKey(res.token);
        return res;
      },
      async () => {
        setApiKey("mock-session-token");
        return {
          token: "mock-session-token",
          username,
          expires_at: new Date(Date.now() + 86_400_000).toISOString(),
        };
      },
    ),
  /** Invalidate the session server-side, then drop the local token. */
  logout: () =>
    call<{ status: string }>(
      async () => {
        try {
          return await request<{ status: string }>("/api/auth/logout", { method: "POST" });
        } finally {
          setApiKey(null);
        }
      },
      async () => {
        setApiKey(null);
        return { status: "ok" };
      },
    ),
  /** Identify the current session; used to verify a stored token on boot. */
  me: () =>
    call<MeResult>(
      () => request("/api/auth/me"),
      async () => ({ username: "demo", key_id: "mock" }),
    ),

  // ---- System ----
  health: () => call<Health>(() => request("/api/health"), mockApi.health),

  // ---- Stats ----
  stats: () => call<StatsSnapshot>(() => request("/api/stats"), mockApi.stats),
  statsHistory: (limit = 120) =>
    call<{ history: StatsSnapshot[] }>(
      () => request(`/api/stats/history?limit=${limit}`),
      () => mockApi.statsHistory(limit),
    ),

  // ---- Tests ----
  listTests: () =>
    call<{ tests: TestInstance[] }>(() => request("/api/tests"), mockApi.listTests),
  startTest: (req: StartTestRequest) =>
    call(
      () => request<{ id: string; instance: TestInstance }>("/api/tests/start", {
        method: "POST",
        body: req,
      }),
      () => mockApi.startTest(req),
    ),
  stopTest: (id: string) =>
    call(
      () => request(`/api/tests/${encodeURIComponent(id)}/stop`, { method: "POST" }),
      () => mockApi.stopTest(id),
    ),
  updateRate: (id: string, call_rate: number) =>
    call(
      () =>
        request(`/api/tests/${encodeURIComponent(id)}/rate`, {
          method: "POST",
          body: { call_rate },
        }),
      () => mockApi.updateRate(id, call_rate),
    ),
  removeTest: (id: string) =>
    call(
      () => request(`/api/tests/${encodeURIComponent(id)}`, { method: "DELETE" }),
      () => mockApi.removeTest(id),
    ),
  stopAll: () =>
    call(
      () => request("/api/tests/stop-all", { method: "POST" }),
      mockApi.stopAll,
    ),

  // ---- Scenarios ----
  listScenarios: () =>
    call<{ scenarios: Scenario[] }>(
      () => request("/api/scenarios"),
      mockApi.listScenarios,
    ),
  getScenario: (name: string) =>
    call<{ name: string; content: string }>(
      () => request(`/api/scenarios/${encodeURIComponent(name)}`),
      () => mockApi.getScenario(name),
    ),
  saveScenario: (name: string, xml_content: string, description = "", mode = "uac") =>
    call(
      () =>
        request("/api/scenarios", {
          method: "POST",
          body: { name, xml_content, description, mode },
        }),
      () => mockApi.saveScenario(name, xml_content),
    ),
  deleteScenario: (name: string) =>
    call(
      () => request(`/api/scenarios/${encodeURIComponent(name)}`, { method: "DELETE" }),
      () => mockApi.deleteScenario(name),
    ),

  // ---- Connectors ----
  listConnectors: () =>
    call<{ connectors: Connector[] }>(
      () => request("/api/connectors"),
      mockApi.listConnectors,
    ),
  createConnector: (req: ConnectorRequest) =>
    call(
      () => request("/api/connectors", { method: "POST", body: req }),
      () => mockApi.createConnector(req),
    ),
  deleteConnector: (name: string) =>
    call(
      () => request(`/api/connectors/${encodeURIComponent(name)}`, { method: "DELETE" }),
      () => mockApi.deleteConnector(name),
    ),

  // ---- History ----
  history: (limit = 50) =>
    call<{ history: TestRun[] }>(
      () => request(`/api/history?limit=${limit}`),
      () => mockApi.history(limit),
    ),

  // ---- Loop campaigns (gencall/api/loops.py) ----
  // Not part of the in-browser mock surface — these always hit the real worker.
  listLoops: () =>
    call<{ campaigns: LoopCampaign[] }>(
      () => request("/api/loops"),
      async () => ({ campaigns: LOOP_CAMPAIGNS }),
    ),
  getLoop: (id: string) =>
    request<LoopCampaign>(`/api/loops/${encodeURIComponent(id)}`),
  startLoop: (req: StartLoopRequest) =>
    request<{ status: string; campaign: LoopCampaign }>("/api/loops", {
      method: "POST",
      body: req,
    }),
  /** Past + present loop runs with their final stats, newest first (History tab). */
  loopHistory: () => request<{ runs: LoopCampaign[] }>("/api/loops/history"),
  /** Fleet-wide loops: this box + every remote worker, each tagged with `box`
   *  and carrying loop_stats. Powers the Loops page so remote loops are visible. */
  listLoopsFleet: () =>
    call<{ campaigns: LoopCampaign[] }>(
      () => request("/api/loops/fleet"),
      async () => ({ campaigns: LOOP_CAMPAIGNS }),
    ),
  /** Per-node CPU/RAM across the fleet (Fleet page). Polls each remote worker. */
  listFleetResources: () => request<FleetResourcesResponse>("/api/fleet/resources"),
  /** Fleet-wide inbound trust whitelist (controller-managed; Config page). */
  getFleetTrust: () => request<FleetTrust>("/api/fleet/config/trust"),
  /** Persist the fleet trust whitelist and push it to every enabled worker. */
  setFleetTrust: (body: FleetTrust) =>
    request<FleetTrustResult>("/api/fleet/config/trust", { method: "POST", body }),
  /** Stop a campaign on whichever box runs it (box = "local" or a worker url). */
  stopLoopFleet: (campaign_id: string, box: string) =>
    request<{ status: string }>("/api/loops/fleet-stop", {
      method: "POST",
      body: { campaign_id, box },
    }),
  stopLoop: (id: string) =>
    request<{ status: string; campaign: LoopCampaign }>(
      `/api/loops/${encodeURIComponent(id)}/stop`,
      { method: "POST" },
    ),

  // ---- On-demand trace (pcap) capture for a running loop ----
  // Controller "fleet-capture" endpoints route by box ("local" or a worker url);
  // tcpdump runs on the worker, the file is pulled on explicit download only.
  /** Start a tcpdump capture for a running loop on its box. */
  startCapture: (campaign_id: string, box: string) =>
    request<{ status: string; capture: CaptureInfo }>("/api/loops/fleet-capture/start", {
      method: "POST",
      body: { campaign_id, box },
    }),
  /** Stop a running capture. */
  stopCapture: (campaign_id: string, box: string, capture_id: string) =>
    request<{ status: string; capture: CaptureInfo }>("/api/loops/fleet-capture/stop", {
      method: "POST",
      body: { campaign_id, box, capture_id },
    }),
  /** List a loop's captures (running + stopped, until deleted). */
  listCaptures: (campaign_id: string, box: string) =>
    request<{ captures: CaptureInfo[] }>(
      `/api/loops/fleet-capture/list?campaign_id=${encodeURIComponent(campaign_id)}&box=${encodeURIComponent(box)}`,
    ),
  /** Delete a capture's file from the worker. */
  deleteCapture: (campaign_id: string, box: string, capture_id: string) =>
    request<{ status: string }>("/api/loops/fleet-capture/delete", {
      method: "DELETE",
      body: { campaign_id, box, capture_id },
    }),
  /** Stream a capture's .pcap to the browser (authenticated download). */
  downloadCapture: (campaign_id: string, box: string, capture_id: string) =>
    downloadAuthed(
      `/api/loops/fleet-capture/download?campaign_id=${encodeURIComponent(campaign_id)}` +
        `&box=${encodeURIComponent(box)}&capture_id=${encodeURIComponent(capture_id)}`,
      `${campaign_id}_${capture_id}.pcap`,
    ),

  // ---- Sale zones (Country -> Zone -> Code pickers on the Nodes page) ----
  saleZones: () =>
    call<SaleZonesResponse>(() => request("/api/sale-zones"), async () => SALE_ZONES),
  createSaleZone: (req: SaleZoneCreate) =>
    request<{ status: string; sale_zone: SaleZoneRow }>("/api/sale-zones", {
      method: "POST",
      body: req,
    }),
  deleteSaleZone: (id: number) =>
    request<{ status: string; id: number }>(`/api/sale-zones/${id}`, {
      method: "DELETE",
    }),

  // ---- Nodes (source-IP servers, each carrying its own number pool) ----
  listServers: () =>
    call<{ servers: Server[] }>(() => request("/api/servers"), async () => ({ servers: SERVERS })),
  sourceIps: () =>
    call<{ source_ips: string[] }>(
      () => request("/api/source-ips"),
      async () => ({ source_ips: ["10.20.1.11", "10.20.1.12", "10.20.1.13", "10.20.1.14"] }),
    ),
  createServer: (req: ServerRequest) =>
    request<{ status: string; server: Server }>("/api/servers", {
      method: "POST",
      body: req,
    }),
  generateServerPool: (id: number, req: GeneratePoolRequest) =>
    request<{ status: string; server: Server }>(`/api/servers/${id}/generate`, {
      method: "POST",
      body: req,
    }),
  updateServer: (
    id: number,
    req: Partial<{
      name: string;
      description: string;
      group_id: number | null;
      enabled: boolean;
      api_url: string;
      api_key: string;
    }>,
  ) =>
    request<{ status: string; server: Server }>(`/api/servers/${id}`, {
      method: "PUT",
      body: req,
    }),
  deleteServer: (id: number) =>
    request<{ status: string; id: number }>(`/api/servers/${id}`, {
      method: "DELETE",
    }),

  // ---- Node groups (group nodes by route; start/stop a whole group at once) ----
  listNodeGroups: () =>
    call<{ groups: NodeGroup[] }>(
      () => request("/api/node-groups"),
      async () => ({ groups: NODE_GROUPS }),
    ),
  createNodeGroup: (req: NodeGroupRequest) =>
    request<{ status: string; group: NodeGroup }>("/api/node-groups", {
      method: "POST",
      body: req,
    }),
  updateNodeGroup: (id: number, req: NodeGroupRequest) =>
    request<{ status: string; group: NodeGroup }>(`/api/node-groups/${id}`, {
      method: "PUT",
      body: req,
    }),
  deleteNodeGroup: (id: number) =>
    request<{ status: string; id: number }>(`/api/node-groups/${id}`, {
      method: "DELETE",
    }),
  startNodeGroup: (id: number, nodeIds?: number[]) =>
    request<GroupStartResult>(`/api/node-groups/${id}/start`, {
      method: "POST",
      body: nodeIds && nodeIds.length ? { node_ids: nodeIds } : {},
    }),
  stopNodeGroup: (id: number) =>
    request<{ status: string; group: string; stopped: number }>(
      `/api/node-groups/${id}/stop`,
      { method: "POST" },
    ),

  // ---- Loop presets (saved recipes; Run on a node or a group) ----
  listLoopPresets: () =>
    call<{ presets: LoopPreset[] }>(
      () => request("/api/loop-presets"),
      async () => ({ presets: LOOP_PRESETS }),
    ),
  createLoopPreset: (req: LoopPresetRequest) =>
    request<{ status: string; preset: LoopPreset }>("/api/loop-presets", {
      method: "POST",
      body: req,
    }),
  updateLoopPreset: (id: number, req: LoopPresetRequest) =>
    request<{ status: string; preset: LoopPreset }>(`/api/loop-presets/${id}`, {
      method: "PUT",
      body: req,
    }),
  deleteLoopPreset: (id: number) =>
    request<{ status: string; id: number }>(`/api/loop-presets/${id}`, {
      method: "DELETE",
    }),
  /** Launch a saved preset on a node (or fan out across a group). */
  runLoopPreset: (id: number, target: RunPresetRequest) =>
    request<{
      status: string;
      preset: string;
      started: number;
      total: number;
      results: GroupStartResult["results"];
    }>(`/api/loop-presets/${id}/run`, { method: "POST", body: target }),

  // ---- Traffic calculator (size a diurnal campaign from a minutes target) ----
  /** Size peak/avg CPS + peak concurrency from a daily minutes target + ACD. */
  trafficCalc: (body: { target_minutes: number; acd_s: number; profile: Partial<TrafficProfile> }) =>
    request<TrafficCalcResult>("/api/loops/traffic-calc", { method: "POST", body }),

  /** Probe a remote worker's health WITHOUT saving — the node form's
   *  "Test connection" button (POST /api/servers/check-worker). */
  checkWorker: (api_url: string, api_key: string) =>
    request<WorkerCheck>("/api/servers/check-worker", {
      method: "POST",
      body: { api_url, api_key },
    }),
};
