/* ============================================================================
   Typed REST client for the VanDorial controller API (fleet design §4).

   The browser only ever talks to the controller (design §2): one login = one
   controller admin key. We therefore reuse the existing single-node auth path
   verbatim — same `X-API-Key` header, same localStorage key, same BASE "" /
   same-origin (Vite proxies /api in dev). `getApiKey` / `setApiKey` from
   ../lib/api.ts are re-exported so callers have one source of truth.

   All calls go through one `request()` so error handling + the mock fallback
   live in a single place (mirrors ../lib/api.ts). Node-scoped requests use the
   controller proxy: /api/nodes/{id}/proxy/{rest}.
   ============================================================================ */
import { ApiError, getApiKey, setApiKey } from "../lib/api";
import type { StatsSnapshot } from "../lib/types";
import type {
  CreateGroupRequest,
  CreateNodeRequest,
  FleetLaunchRequest,
  FleetLaunchResponse,
  FleetRunView,
  FleetStats,
  GroupView,
  NodeView,
  UpdateGroupRequest,
  UpdateNodeRequest,
} from "./types";
import { fleetMock, FLEET_MOCK_ENABLED } from "./fleetMock";

export { ApiError, getApiKey, setApiKey };

const BASE = ""; // same origin; Vite proxies /api in dev

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
    // Controller unreachable. In mock mode we never get here (intercepted
    // below), so surface a clear, actionable error.
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

/* When FLEET_MOCK_ENABLED, route through the in-browser fleet simulator so the
   fleet console is fully demoable without a controller. Real calls otherwise. */
function call<T>(real: () => Promise<T>, mock: () => Promise<T>): Promise<T> {
  return FLEET_MOCK_ENABLED ? mock() : real();
}

export const fleetApi = {
  // ---- Nodes ----
  listNodes: () =>
    call<{ nodes: NodeView[] }>(() => request("/api/nodes"), fleetMock.listNodes),
  createNode: (req: CreateNodeRequest) =>
    call<NodeView>(
      () => request("/api/nodes", { method: "POST", body: req }),
      () => fleetMock.createNode(req),
    ),
  updateNode: (id: number, req: UpdateNodeRequest) =>
    call<NodeView>(
      () => request(`/api/nodes/${id}`, { method: "PUT", body: req }),
      () => fleetMock.updateNode(id, req),
    ),
  deleteNode: (id: number) =>
    call<{ status: "deleted"; id: number }>(
      () => request(`/api/nodes/${id}`, { method: "DELETE" }),
      () => fleetMock.deleteNode(id),
    ),
  checkNode: (id: number) =>
    call<NodeView>(
      () => request(`/api/nodes/${id}/check`, { method: "POST" }),
      () => fleetMock.checkNode(id),
    ),

  // ---- Groups ----
  listGroups: () =>
    call<{ groups: GroupView[] }>(() => request("/api/groups"), fleetMock.listGroups),
  createGroup: (req: CreateGroupRequest) =>
    call<GroupView>(
      () => request("/api/groups", { method: "POST", body: req }),
      () => fleetMock.createGroup(req),
    ),
  updateGroup: (id: number, req: UpdateGroupRequest) =>
    call<GroupView>(
      () => request(`/api/groups/${id}`, { method: "PUT", body: req }),
      () => fleetMock.updateGroup(id, req),
    ),
  deleteGroup: (id: number) =>
    call<{ status: "deleted"; id: number }>(
      () => request(`/api/groups/${id}`, { method: "DELETE" }),
      () => fleetMock.deleteGroup(id),
    ),

  // ---- Fleet campaigns ----
  launch: (req: FleetLaunchRequest) =>
    call<FleetLaunchResponse>(
      () => request("/api/fleet/launch", { method: "POST", body: req }),
      () => fleetMock.launch(req),
    ),
  stopRun: (id: number) =>
    call<{ status: string }>(
      () => request(`/api/fleet/${id}/stop`, { method: "POST" }),
      () => fleetMock.stopRun(id),
    ),
  listRuns: (limit = 50) =>
    call<{ runs: FleetRunView[] }>(
      () => request(`/api/fleet/runs?limit=${limit}`),
      () => fleetMock.listRuns(limit),
    ),
  getRun: (id: number) =>
    call<FleetRunView>(
      () => request(`/api/fleet/runs/${id}`),
      () => fleetMock.getRun(id),
    ),

  // ---- Aggregated telemetry ----
  stats: () =>
    call<FleetStats>(() => request("/api/fleet/stats"), fleetMock.stats),
  statsHistory: (limit = 240) =>
    call<{ history: StatsSnapshot[] }>(
      () => request(`/api/fleet/stats/history?limit=${limit}`),
      () => fleetMock.statsHistory(limit),
    ),

  /* ---- Single-node passthrough -----------------------------------------
     Proxy an arbitrary worker request through the controller, which injects
     that node's key. Lets existing console pages (Campaigns, Scenarios,
     Connectors, Console…) operate against one selected node. `rest` is the
     worker path WITHOUT a leading slash, e.g. "api/tests" or "api/stats". */
  proxy: <T>(
    nodeId: number,
    rest: string,
    init?: Omit<RequestInit, "body"> & { body?: unknown },
  ) =>
    call<T>(
      () => request<T>(`/api/nodes/${nodeId}/proxy/${rest.replace(/^\/+/, "")}`, init),
      () => fleetMock.proxy<T>(nodeId, rest, init),
    ),
};
