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
  LoopCampaign,
  LoopPreset,
  LoopPresetRequest,
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

export const api = {
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
  listLoops: () => request<{ campaigns: LoopCampaign[] }>("/api/loops"),
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
  listLoopsFleet: () => request<{ campaigns: LoopCampaign[] }>("/api/loops/fleet"),
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
  saleZones: () => request<SaleZonesResponse>("/api/sale-zones"),
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
  listServers: () => request<{ servers: Server[] }>("/api/servers"),
  sourceIps: () => request<{ source_ips: string[] }>("/api/source-ips"),
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
  listNodeGroups: () => request<{ groups: NodeGroup[] }>("/api/node-groups"),
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
  listLoopPresets: () => request<{ presets: LoopPreset[] }>("/api/loop-presets"),
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
