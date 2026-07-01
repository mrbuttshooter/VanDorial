/* ============================================================================
   In-browser mock controller — a stateful fleet simulator.

   Simulates ~12 GenCall worker nodes across 3 groups with live aggregate /
   per-group / per-node telemetry, plus full inventory + group + campaign CRUD,
   so the fleet console is fully demoable with no controller running.

   Gated exactly like lib/mock.ts: enabled in dev by default, disabled in
   production builds. Override with VITE_MOCK=true|false. fleetApi.ts wires its
   mock fallback to FLEET_MOCK_ENABLED, mirroring lib/api.ts → MOCK_ENABLED.
   ============================================================================ */
import type { StatsSnapshot } from "../lib/types";
import type {
  CreateGroupRequest,
  CreateNodeRequest,
  FleetDispatchResult,
  FleetLaunchRequest,
  FleetLaunchResponse,
  FleetRunView,
  FleetStats,
  GroupView,
  NodeView,
  UpdateGroupRequest,
  UpdateNodeRequest,
} from "./types";

export const FLEET_MOCK_ENABLED =
  import.meta.env.VITE_MOCK === "true" ||
  (import.meta.env.DEV && import.meta.env.VITE_MOCK !== "false");

const delay = (ms = 90) => new Promise((r) => setTimeout(r, ms));
const rand = (a: number, b: number) => a + Math.random() * (b - a);
const now = () => Date.now() / 1000;
const sum = <T>(arr: T[], f: (x: T) => number) => arr.reduce((a, x) => a + f(x), 0);
const avg = <T>(arr: T[], f: (x: T) => number) => (arr.length ? sum(arr, f) / arr.length : 0);

/* ---- Internal node model ------------------------------------------------ */
interface SimNode {
  id: number;
  name: string;
  address: string;
  group_id: number | null;
  api_key: string;
  enabled: boolean;
  online: boolean;
  last_seen: number | null; // epoch seconds
  version: string | null;
  error: string;
  // Live simulation state — present only while this node is running traffic.
  running: boolean;
  targetRate: number; // cps this node is aiming for
  transport: "udp" | "tcp" | "tls";
  startedAt: number;
  stats: StatsSnapshot;
}

interface SimGroup {
  id: number;
  name: string;
  description: string;
}

interface SimRun {
  id: number;
  name: string;
  group_id: number | null;
  node_ids: number[];
  scenario: string;
  destination: { remote_host: string; remote_port?: number; transport?: "udp" | "tcp" | "tls" };
  rate_mode: "per_node" | "total";
  rate_value: number;
  status: FleetRunView["status"];
  started_at: number | null;
  completed_at: number | null;
  results: FleetDispatchResult[];
}

function blankStats(): StatsSnapshot {
  return {
    timestamp: now(),
    active_instances: 0,
    total_calls: 0,
    successful_calls: 0,
    failed_calls: 0,
    current_calls: 0,
    calls_per_second: 0,
    avg_response_time_ms: 0,
    success_rate: 100,
  };
}

/* ---- Seed: 3 groups, ~12 nodes ----------------------------------------- */
const groups: SimGroup[] = [
  { id: 1, name: "NY-edge", description: "New York edge generators (north-east POP)" },
  { id: 2, name: "LON-carrier", description: "London carrier-trunk load nodes" },
  { id: 3, name: "FRA-ims", description: "Frankfurt IMS / registration soak nodes" },
];

let nodeSeq = 0;
let groupSeq = groups.length;
let runSeq = 0;

function makeNode(
  name: string,
  group_id: number | null,
  octet: number,
  opts: Partial<SimNode> = {},
): SimNode {
  const id = ++nodeSeq;
  const transports: SimNode["transport"][] = ["udp", "udp", "tcp", "tls"];
  return {
    id,
    name,
    address: `https://10.20.${group_id ?? 9}.${octet}:8080`,
    group_id,
    api_key: `nodekey-${Math.random().toString(16).slice(2, 12)}`,
    enabled: true,
    online: true,
    last_seen: now() - rand(1, 4),
    version: "2.0.0",
    error: "",
    running: false,
    targetRate: 0,
    transport: transports[id % transports.length],
    startedAt: now(),
    stats: blankStats(),
    ...opts,
  };
}

const nodes: SimNode[] = [
  makeNode("ny-gen-01", 1, 11, { running: true, targetRate: 180 }),
  makeNode("ny-gen-02", 1, 12, { running: true, targetRate: 165 }),
  makeNode("ny-gen-03", 1, 13, { running: true, targetRate: 172 }),
  makeNode("ny-gen-04", 1, 14, { enabled: false, online: false, last_seen: now() - 3600, version: null }),
  makeNode("lon-gen-01", 2, 21, { running: true, targetRate: 210 }),
  makeNode("lon-gen-02", 2, 22, { running: true, targetRate: 198 }),
  makeNode("lon-gen-03", 2, 23, { running: true, targetRate: 205 }),
  makeNode("lon-gen-04", 2, 24, { running: true, targetRate: 190 }),
  makeNode("fra-gen-01", 3, 31, { running: true, targetRate: 240 }),
  makeNode("fra-gen-02", 3, 32, { online: false, error: "connect timeout (probe)", last_seen: now() - 240, version: null }),
  makeNode("fra-gen-03", 3, 33, { running: true, targetRate: 232 }),
  makeNode("spare-gen-01", null, 41, { running: false }),
];

const runs: SimRun[] = [];
const aggHistory: StatsSnapshot[] = [];

/* ---- Live simulation tick ---------------------------------------------- */
let lastTick = now();

function tickNode(node: SimNode, dt: number) {
  if (!node.online || !node.running) {
    // Decay current calls / cps when idle so the grid settles to zero.
    node.stats.calls_per_second = +(node.stats.calls_per_second * 0.5).toFixed(2);
    node.stats.current_calls = Math.round(node.stats.current_calls * 0.5);
    node.stats.active_instances = node.running ? 1 : 0;
    return;
  }
  const s = node.stats;
  // Ease cps toward target with jitter — looks like a real ramp.
  s.calls_per_second += (node.targetRate - s.calls_per_second) * 0.2;
  const jitter = rand(-0.08, 0.08) * s.calls_per_second;
  const cps = Math.max(0, s.calls_per_second + jitter);
  const newCalls = cps * dt;
  const failChance = node.transport === "tls" ? 0.012 : 0.02;
  const failed = Math.round(newCalls * rand(0, failChance * 2));
  s.total_calls += Math.round(newCalls);
  s.failed_calls += failed;
  s.successful_calls = s.total_calls - s.failed_calls;
  s.current_calls = Math.round(cps * rand(2.5, 4));
  s.calls_per_second = +cps.toFixed(2);
  s.avg_response_time_ms = +(
    s.avg_response_time_ms * 0.85 +
    rand(26, node.transport === "tls" ? 95 : 70) * 0.15
  ).toFixed(2);
  s.active_instances = 1;
  s.timestamp = +now().toFixed(1);
  s.success_rate = s.total_calls
    ? +((s.successful_calls / s.total_calls) * 100).toFixed(2)
    : 100;
}

function rollup(snaps: StatsSnapshot[]): StatsSnapshot {
  const out: StatsSnapshot = {
    timestamp: +now().toFixed(1),
    active_instances: sum(snaps, (x) => x.active_instances),
    total_calls: sum(snaps, (x) => x.total_calls),
    successful_calls: sum(snaps, (x) => x.successful_calls),
    failed_calls: sum(snaps, (x) => x.failed_calls),
    current_calls: sum(snaps, (x) => x.current_calls),
    calls_per_second: +sum(snaps, (x) => x.calls_per_second).toFixed(2),
    avg_response_time_ms: snaps.length ? +avg(snaps, (x) => x.avg_response_time_ms).toFixed(2) : 0,
    success_rate: 100,
  };
  out.success_rate = out.total_calls
    ? +((out.successful_calls / out.total_calls) * 100).toFixed(2)
    : 100;
  return out;
}

function onlineSnaps(): StatsSnapshot[] {
  return nodes.filter((n) => n.online).map((n) => n.stats);
}

function tick() {
  const t = now();
  const dt = Math.min(2, t - lastTick);
  lastTick = t;
  for (const node of nodes) tickNode(node, dt);
  const agg = rollup(onlineSnaps());
  aggHistory.push(agg);
  if (aggHistory.length > 600) aggHistory.shift();

  // Drive a couple of nodes per group so the dashboard is alive on first paint.
  // (Only matters until the first real campaign launches.)
}

/* Seed: kick a representative subset of nodes into running so the fleet looks
   busy immediately, then pre-fill ~3 minutes of aggregate history. */
function seedRunning() {
  const busy = ["ny-gen-01", "ny-gen-02", "lon-gen-01", "lon-gen-02", "lon-gen-03", "fra-gen-01"];
  for (const node of nodes) {
    if (busy.includes(node.name) && node.online) {
      node.running = true;
      node.targetRate = rand(20, 55);
      node.startedAt = now();
    }
  }
}

if (FLEET_MOCK_ENABLED) {
  seedRunning();
  lastTick = now() - 180;
  for (let i = 0; i < 180; i++) tick();
  lastTick = now();
  setInterval(tick, 1000);
}

/* ---- View builders ------------------------------------------------------ */
function groupName(id: number | null): string | null {
  if (id == null) return null;
  return groups.find((g) => g.id === id)?.name ?? null;
}

function toNodeView(n: SimNode): NodeView {
  return {
    id: n.id,
    name: n.name,
    address: n.address,
    group_id: n.group_id,
    group_name: groupName(n.group_id),
    enabled: n.enabled,
    online: n.online,
    last_seen: n.last_seen != null ? new Date(n.last_seen * 1000).toISOString() : null,
    version: n.version,
    active_tests: n.online && n.running ? 1 : 0,
    error: n.error,
  };
}

function toGroupView(g: SimGroup): GroupView {
  const members = nodes.filter((n) => n.group_id === g.id);
  return {
    id: g.id,
    name: g.name,
    description: g.description,
    node_ids: members.map((n) => n.id),
    online_count: members.filter((n) => n.online).length,
    total_count: members.length,
  };
}

function toRunView(r: SimRun): FleetRunView {
  return {
    id: r.id,
    name: r.name,
    group_id: r.group_id,
    node_ids: r.node_ids,
    scenario: r.scenario,
    destination: r.destination,
    rate_mode: r.rate_mode,
    rate_value: r.rate_value,
    status: r.status,
    started_at: r.started_at != null ? new Date(r.started_at * 1000).toISOString() : null,
    completed_at: r.completed_at != null ? new Date(r.completed_at * 1000).toISOString() : null,
    results: r.results,
  };
}

/* Resolve launch targets: explicit node_ids win, else a group's members. */
function resolveTargets(req: FleetLaunchRequest): SimNode[] {
  if (req.node_ids?.length) {
    const wanted = new Set(req.node_ids);
    return nodes.filter((n) => wanted.has(n.id));
  }
  if (req.group_id != null) return nodes.filter((n) => n.group_id === req.group_id);
  return [];
}

/* Distribute a campaign rate across online targets per the §5 rate model. */
function perNodeRates(req: FleetLaunchRequest, onlineTargets: SimNode[]): Map<number, number> {
  const out = new Map<number, number>();
  if (req.rate.mode === "total" && onlineTargets.length) {
    const base = Math.floor(req.rate.value / onlineTargets.length);
    let remainder = req.rate.value - base * onlineTargets.length;
    for (const n of onlineTargets) {
      out.set(n.id, base + (remainder > 0 ? 1 : 0));
      if (remainder > 0) remainder--;
    }
  } else {
    for (const n of onlineTargets) out.set(n.id, req.rate.value);
  }
  return out;
}

/* ---- Mock controller API surface (mirrors fleetApi.ts) ----------------- */
export const fleetMock = {
  // ---- Nodes ----
  async listNodes(): Promise<{ nodes: NodeView[] }> {
    await delay(60);
    return { nodes: nodes.map(toNodeView) };
  },
  async createNode(req: CreateNodeRequest): Promise<NodeView> {
    await delay(90);
    const n = makeNode(req.name, req.group_id ?? null, 90 + nodes.length, {
      address: req.address,
      api_key: req.api_key,
      enabled: req.enabled ?? true,
      online: req.enabled ?? true,
    });
    nodes.push(n);
    return toNodeView(n);
  },
  async updateNode(id: number, req: UpdateNodeRequest): Promise<NodeView> {
    await delay(80);
    const n = nodes.find((x) => x.id === id);
    if (!n) throw new Error(`node ${id} not found`);
    if (req.name !== undefined) n.name = req.name;
    if (req.address !== undefined) n.address = req.address;
    if (req.group_id !== undefined) n.group_id = req.group_id;
    if (req.api_key !== undefined) n.api_key = req.api_key;
    if (req.enabled !== undefined) {
      n.enabled = req.enabled;
      if (!req.enabled) {
        n.online = false;
        n.running = false;
      }
    }
    return toNodeView(n);
  },
  async deleteNode(id: number): Promise<{ status: "deleted"; id: number }> {
    await delay(70);
    const i = nodes.findIndex((x) => x.id === id);
    if (i >= 0) nodes.splice(i, 1);
    return { status: "deleted", id };
  },
  async checkNode(id: number): Promise<NodeView> {
    await delay(120);
    const n = nodes.find((x) => x.id === id);
    if (!n) throw new Error(`node ${id} not found`);
    // Simulate a probe: enabled nodes come back online most of the time.
    if (n.enabled) {
      const reachable = Math.random() > 0.1;
      n.online = reachable;
      n.error = reachable ? "" : "connect timeout (probe)";
      n.last_seen = reachable ? now() : n.last_seen;
      n.version = reachable ? "2.0.0" : n.version;
    }
    return toNodeView(n);
  },

  // ---- Groups ----
  async listGroups(): Promise<{ groups: GroupView[] }> {
    await delay(60);
    return { groups: groups.map(toGroupView) };
  },
  async createGroup(req: CreateGroupRequest): Promise<GroupView> {
    await delay(80);
    const g: SimGroup = { id: ++groupSeq, name: req.name, description: req.description ?? "" };
    groups.push(g);
    return toGroupView(g);
  },
  async updateGroup(id: number, req: UpdateGroupRequest): Promise<GroupView> {
    await delay(80);
    const g = groups.find((x) => x.id === id);
    if (!g) throw new Error(`group ${id} not found`);
    if (req.name !== undefined) g.name = req.name;
    if (req.description !== undefined) g.description = req.description;
    if (req.node_ids !== undefined) {
      const wanted = new Set(req.node_ids);
      for (const n of nodes) {
        if (wanted.has(n.id)) n.group_id = g.id;
        else if (n.group_id === g.id) n.group_id = null;
      }
    }
    return toGroupView(g);
  },
  async deleteGroup(id: number): Promise<{ status: "deleted"; id: number }> {
    await delay(70);
    const i = groups.findIndex((x) => x.id === id);
    if (i >= 0) groups.splice(i, 1);
    for (const n of nodes) if (n.group_id === id) n.group_id = null;
    return { status: "deleted", id };
  },

  // ---- Fleet campaigns ----
  async launch(req: FleetLaunchRequest): Promise<FleetLaunchResponse> {
    await delay(160);
    const targets = resolveTargets(req);
    const online = targets.filter((n) => n.online && n.enabled);
    const rates = perNodeRates(req, online);
    const dispatched: FleetDispatchResult[] = targets.map((n) => {
      if (!n.online || !n.enabled) {
        return { node_id: n.id, ok: false, error: "node offline" };
      }
      n.running = true;
      n.targetRate = rates.get(n.id) ?? req.rate.value;
      n.startedAt = now();
      n.transport = req.destination.transport ?? n.transport;
      return { node_id: n.id, ok: true, test_id: `test-${Math.random().toString(16).slice(2, 10)}` };
    });

    const anyOk = dispatched.some((d) => d.ok);
    const anyFail = dispatched.some((d) => !d.ok);
    const status: FleetRunView["status"] = !anyOk
      ? "failed"
      : anyFail
        ? "partial"
        : "running";

    const run: SimRun = {
      id: ++runSeq + 5000,
      name: req.name || `fleet-run-${runSeq}`,
      group_id: req.group_id ?? null,
      node_ids: targets.map((n) => n.id),
      scenario: req.scenario,
      destination: req.destination,
      rate_mode: req.rate.mode,
      rate_value: req.rate.value,
      status,
      started_at: now(),
      completed_at: null,
      results: dispatched,
    };
    runs.unshift(run);
    return { fleet_run_id: run.id, dispatched };
  },
  async stopRun(id: number): Promise<{ status: string }> {
    await delay(120);
    const run = runs.find((r) => r.id === id);
    if (run) {
      for (const r of run.results) {
        if (r.ok) {
          const n = nodes.find((x) => x.id === r.node_id);
          if (n) n.running = false;
        }
      }
      run.status = "stopped";
      run.completed_at = now();
    }
    return { status: "stopped" };
  },
  async listRuns(limit: number): Promise<{ runs: FleetRunView[] }> {
    await delay(70);
    return { runs: runs.slice(0, limit).map(toRunView) };
  },
  async getRun(id: number): Promise<FleetRunView> {
    await delay(60);
    const run = runs.find((r) => r.id === id);
    if (!run) throw new Error(`fleet run ${id} not found`);
    return toRunView(run);
  },

  // ---- Aggregated telemetry ----
  async stats(): Promise<FleetStats> {
    await delay(50);
    const per_node: Record<number, StatsSnapshot | null> = {};
    for (const n of nodes) per_node[n.id] = n.online ? { ...n.stats } : null;
    const per_group: Record<number, StatsSnapshot> = {};
    for (const g of groups) {
      const snaps = nodes.filter((n) => n.group_id === g.id && n.online).map((n) => n.stats);
      per_group[g.id] = rollup(snaps);
    }
    return { aggregate: rollup(onlineSnaps()), per_group, per_node };
  },
  async statsHistory(limit: number): Promise<{ history: StatsSnapshot[] }> {
    await delay(60);
    return { history: aggHistory.slice(-limit) };
  },

  /* ---- Single-node passthrough -----------------------------------------
     The real controller proxies to the node's worker API. The mock can't run
     a full per-node worker, so it answers the handful of GETs the console
     needs against the selected node's simulated state, and acks writes. */
  async proxy<T>(
    nodeId: number,
    rest: string,
    init?: Omit<RequestInit, "body"> & { body?: unknown },
  ): Promise<T> {
    await delay(70);
    const n = nodes.find((x) => x.id === nodeId);
    if (!n) throw new Error(`node ${nodeId} not found`);
    const path = rest.replace(/^\/+/, "").split("?")[0];
    const method = (init?.method ?? "GET").toUpperCase();

    if (path === "api/health") {
      return {
        status: n.online ? "ok" : "down",
        version: n.version ?? "unknown",
        name: n.name,
        active_tests: n.running ? 1 : 0,
      } as T;
    }
    if (path === "api/stats") {
      return { ...n.stats } as T;
    }
    if (path === "api/tests") {
      return { tests: [] } as T;
    }
    // Writes / unknown reads: ack so the console flow doesn't error in demo mode.
    return { status: "ok", node_id: nodeId, path, method } as T;
  },
};
