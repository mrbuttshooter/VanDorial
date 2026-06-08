/* ============================================================================
   In-browser mock backend — a small stateful SIP-traffic simulator.

   Enabled in dev by default so the console is fully demoable with no Python
   server. Disabled in production builds, where the app talks to the real
   FastAPI backend that serves it. Override with VITE_MOCK=true|false.
   ============================================================================ */
import type {
  Connector,
  ConnectorRequest,
  Health,
  Scenario,
  StartTestRequest,
  StatsSnapshot,
  TestInstance,
  TestRun,
} from "./types";

export const MOCK_ENABLED =
  import.meta.env.VITE_MOCK === "true" ||
  (import.meta.env.DEV && import.meta.env.VITE_MOCK !== "false");

const delay = (ms = 90) => new Promise((r) => setTimeout(r, ms));
const rand = (a: number, b: number) => a + Math.random() * (b - a);
const now = () => Date.now() / 1000;

interface SimTest extends TestInstance {
  _targetRate: number;
  _startedAt: number;
}

function makeTest(req: StartTestRequest): SimTest {
  const id = req.name || `test-${Math.random().toString(16).slice(2, 10)}`;
  const rate = req.call_rate ?? 1;
  return {
    id,
    scenario_file: `${req.scenario}.xml`,
    remote_host: req.remote_host,
    remote_port: req.remote_port ?? 5060,
    local_port: req.local_port ?? 5060,
    local_ip: req.local_ip || "0.0.0.0",
    mode: "uac",
    transport: req.transport ?? "udp",
    call_rate: rate,
    max_calls: req.max_calls ?? 0,
    call_limit: req.call_limit ?? 10,
    duration: req.duration ?? 0,
    state: "running",
    error_message: "",
    _targetRate: rate,
    _startedAt: now(),
    stats: {
      total_calls: 0,
      successful_calls: 0,
      failed_calls: 0,
      current_calls: 0,
      retransmissions: 0,
      calls_per_second: rate,
      avg_response_time_ms: rand(28, 60),
      uptime_seconds: 0,
      success_rate: 100,
    },
  };
}

/* ---- Seed data ---------------------------------------------------------- */
const SCENARIOS: Scenario[] = [
  { name: "basic_call", path: "templates/basic_call.xml", type: "builtin", description: "INVITE → 200 OK → ACK → BYE. Baseline call setup." },
  { name: "call_with_auth", path: "templates/call_with_auth.xml", type: "builtin", description: "Digest-authenticated INVITE with 401 challenge." },
  { name: "basic_register", path: "templates/basic_register.xml", type: "builtin", description: "REGISTER with expiry refresh." },
  { name: "options_ping", path: "templates/options_ping.xml", type: "builtin", description: "OPTIONS keepalive / reachability probe." },
  { name: "stress_test", path: "templates/stress_test.xml", type: "builtin", description: "High-CPS storm for capacity sweeps." },
  { name: "uas_answer", path: "templates/uas_answer.xml", type: "builtin", description: "UAS leg — answer inbound and hold." },
  { name: "uas_full", path: "templates/uas_full.xml", type: "builtin", description: "Full UAS dialog with media + BYE handling." },
];

const SCENARIO_XML = `<?xml version="1.0" encoding="ISO-8859-1" ?>
<!-- GenCall scenario: basic_call -->
<scenario name="Basic Call">
  <send retrans="500">
    <![CDATA[
      INVITE sip:[service]@[remote_ip]:[remote_port] SIP/2.0
      Via: SIP/2.0/[transport] [local_ip]:[local_port];branch=[branch]
      From: sipp <sip:sipp@[local_ip]:[local_port]>;tag=[call_number]
      To: sut <sip:[service]@[remote_ip]:[remote_port]>
      Call-ID: [call_id]
      CSeq: 1 INVITE
      Contact: sip:sipp@[local_ip]:[local_port]
      Content-Type: application/sdp
      Content-Length: [len]
    ]]>
  </send>
  <recv response="100" optional="true"/>
  <recv response="180" optional="true"/>
  <recv response="200" rtd="true"/>
  <send><![CDATA[ ACK sip:[service]@[remote_ip]:[remote_port] SIP/2.0 ]]></send>
  <pause milliseconds="3000"/>
  <send retrans="500"><![CDATA[ BYE sip:[service]@[remote_ip]:[remote_port] SIP/2.0 ]]></send>
  <recv response="200"/>
</scenario>`;

let connectors: Connector[] = [
  { id: 1, name: "lab-sbc-edge", description: "Lab Oracle SBC, north edge", local_ip: "10.20.4.11", local_port: 5060, remote_ip: "10.20.8.40", remote_port: 5060, transport: "udp", auth_user: "gencall", enabled: true, created_at: "2026-05-12T09:14:00", updated_at: "2026-06-01T11:02:00" },
  { id: 2, name: "carrier-a-tls", description: "Carrier A SIP trunk (TLS)", local_ip: "10.20.4.11", local_port: 5061, remote_ip: "203.0.113.25", remote_port: 5061, transport: "tls", auth_user: "trunk_a", enabled: true, created_at: "2026-04-30T15:40:00", updated_at: "2026-05-28T08:20:00" },
  { id: 3, name: "ims-pcscf", description: "IMS P-CSCF reg path", local_ip: "10.20.4.12", local_port: 5060, remote_ip: "10.30.1.5", remote_port: 5060, transport: "tcp", auth_user: "", enabled: false, created_at: "2026-03-18T10:00:00", updated_at: "2026-03-18T10:00:00" },
];

const runHistory: TestRun[] = buildHistory();

function buildHistory(): TestRun[] {
  const names = ["basic_call", "call_with_auth", "stress_test", "options_ping", "basic_register"];
  const out: TestRun[] = [];
  for (let i = 0; i < 14; i++) {
    const total = Math.round(rand(200, 12000));
    const failRate = Math.random() < 0.2 ? rand(4, 22) : rand(0, 3);
    const failed = Math.round((total * failRate) / 100);
    const startedMs = Date.now() - i * rand(3.6e6, 9e6) - 6e5;
    const dur = Math.round(rand(40, 900));
    const status: TestRun["status"] = failRate > 15 ? "failed" : "completed";
    out.push({
      id: 1000 - i,
      name: `run-${(1000 - i).toString(16)}`,
      connector_name: connectors[i % connectors.length].name,
      scenario_name: names[i % names.length],
      status,
      call_rate: Math.round(rand(2, 80)),
      max_calls: 0,
      call_limit: Math.round(rand(10, 200)),
      duration: dur,
      total_calls: total,
      successful_calls: total - failed,
      failed_calls: failed,
      avg_response_time_ms: rand(24, 120),
      error_message: status === "failed" ? "success rate below 90% threshold" : "",
      started_at: new Date(startedMs).toISOString(),
      completed_at: new Date(startedMs + dur * 1000).toISOString(),
      created_at: new Date(startedMs).toISOString(),
    });
  }
  return out;
}

/* ---- Live simulation tick ---------------------------------------------- */
const tests = new Map<string, SimTest>();
const history: StatsSnapshot[] = [];
let lastTick = now();

// Seed two running tests so the dashboard is alive on first paint.
[
  makeTest({ scenario: "basic_call", remote_host: "10.20.8.40", call_rate: 45, name: "edge-soak" }),
  makeTest({ scenario: "call_with_auth", remote_host: "203.0.113.25", transport: "tls", call_rate: 18, name: "carrier-a-verify" }),
].forEach((t) => tests.set(t.id, t));

function tick() {
  const t = now();
  const dt = Math.min(2, t - lastTick);
  lastTick = t;

  for (const test of tests.values()) {
    if (test.state !== "running") continue;
    // Rate eases toward its target with jitter — looks like a real ramp.
    test.call_rate += (test._targetRate - test.call_rate) * 0.2;
    const jitter = rand(-0.08, 0.08) * test.call_rate;
    const cps = Math.max(0, test.call_rate + jitter);
    const newCalls = cps * dt;
    const failChance = test.transport === "tls" ? 0.012 : 0.02;
    const failed = Math.round(newCalls * rand(0, failChance * 2));
    test.stats.total_calls += Math.round(newCalls);
    test.stats.failed_calls += failed;
    test.stats.successful_calls = test.stats.total_calls - test.stats.failed_calls;
    test.stats.current_calls = Math.round(cps * rand(2.5, 4));
    test.stats.retransmissions += Math.round(newCalls * rand(0, 0.03));
    test.stats.calls_per_second = +cps.toFixed(2);
    test.stats.avg_response_time_ms = +(
      test.stats.avg_response_time_ms * 0.85 +
      rand(26, test.transport === "tls" ? 95 : 70) * 0.15
    ).toFixed(2);
    test.stats.uptime_seconds = +(t - test._startedAt).toFixed(1);
    test.stats.success_rate = test.stats.total_calls
      ? +((test.stats.successful_calls / test.stats.total_calls) * 100).toFixed(2)
      : 100;
  }

  const running = [...tests.values()].filter((x) => x.state === "running");
  const agg: StatsSnapshot = {
    timestamp: +t.toFixed(1),
    active_instances: running.length,
    total_calls: sum(running, (x) => x.stats.total_calls),
    successful_calls: sum(running, (x) => x.stats.successful_calls),
    failed_calls: sum(running, (x) => x.stats.failed_calls),
    current_calls: sum(running, (x) => x.stats.current_calls),
    calls_per_second: +sum(running, (x) => x.stats.calls_per_second).toFixed(2),
    avg_response_time_ms: running.length
      ? +(avg(running, (x) => x.stats.avg_response_time_ms)).toFixed(2)
      : 0,
    success_rate: 0,
  };
  agg.success_rate = agg.total_calls
    ? +((agg.successful_calls / agg.total_calls) * 100).toFixed(2)
    : 100;
  history.push(agg);
  if (history.length > 600) history.shift();
}

const sum = <T,>(arr: T[], f: (x: T) => number) => arr.reduce((a, x) => a + f(x), 0);
const avg = <T,>(arr: T[], f: (x: T) => number) => (arr.length ? sum(arr, f) / arr.length : 0);

// Pre-fill ~3 minutes of history so charts aren't empty on load.
if (MOCK_ENABLED) {
  lastTick = now() - 180;
  for (let i = 0; i < 180; i++) tick();
  lastTick = now();
  setInterval(tick, 1000);
}

function currentAgg(): StatsSnapshot {
  return history[history.length - 1] ?? {
    timestamp: now(), active_instances: 0, total_calls: 0, successful_calls: 0,
    failed_calls: 0, current_calls: 0, calls_per_second: 0, avg_response_time_ms: 0,
    success_rate: 100,
  };
}

const strip = (t: SimTest): TestInstance => {
  const { _targetRate: _t, _startedAt: _s, ...rest } = t;
  void _t; void _s;
  return rest;
};

/* ---- Mock API surface (mirrors lib/api.ts) ----------------------------- */
export const mockApi = {
  async health(): Promise<Health> {
    await delay(40);
    return {
      status: "ok",
      version: "2.0.0-mock",
      name: "GenCall",
      active_tests: [...tests.values()].filter((t) => t.state === "running").length,
    };
  },
  async stats(): Promise<StatsSnapshot> {
    await delay(40);
    return currentAgg();
  },
  async statsHistory(limit: number) {
    await delay(50);
    return { history: history.slice(-limit) };
  },
  async listTests() {
    await delay(60);
    return { tests: [...tests.values()].map(strip) };
  },
  async startTest(req: StartTestRequest) {
    await delay(120);
    const t = makeTest(req);
    tests.set(t.id, t);
    return { id: t.id, instance: strip(t) };
  },
  async stopTest(id: string) {
    await delay(80);
    const t = tests.get(id);
    if (t) t.state = "stopped";
    return { status: "stopped", id };
  },
  async updateRate(id: string, call_rate: number) {
    await delay(60);
    const t = tests.get(id);
    if (t) t._targetRate = call_rate;
    return { status: "updated", id, call_rate };
  },
  async removeTest(id: string) {
    await delay(60);
    tests.delete(id);
    return { status: "removed", id };
  },
  async stopAll() {
    await delay(100);
    for (const t of tests.values()) t.state = "stopped";
    return { status: "all_stopped" };
  },
  async listScenarios() {
    await delay(50);
    return { scenarios: SCENARIOS };
  },
  async getScenario(name: string) {
    await delay(50);
    return { name, content: SCENARIO_XML.replace("basic_call", name) };
  },
  async saveScenario(name: string, _xml: string) {
    await delay(80);
    void _xml;
    if (!SCENARIOS.find((s) => s.name === name)) {
      SCENARIOS.push({ name, path: `custom/${name}.xml`, type: "custom", description: "Custom scenario" });
    }
    return { status: "saved", name, path: `custom/${name}.xml` };
  },
  async deleteScenario(name: string) {
    await delay(60);
    const i = SCENARIOS.findIndex((s) => s.name === name && s.type === "custom");
    if (i >= 0) SCENARIOS.splice(i, 1);
    return { status: "deleted", name };
  },
  async listConnectors() {
    await delay(60);
    return { connectors };
  },
  async createConnector(req: ConnectorRequest) {
    await delay(90);
    const c: Connector = {
      id: Math.max(0, ...connectors.map((x) => x.id)) + 1,
      name: req.name,
      description: req.description ?? "",
      local_ip: req.local_ip,
      local_port: req.local_port ?? 5060,
      remote_ip: req.remote_ip,
      remote_port: req.remote_port ?? 5060,
      transport: req.transport ?? "udp",
      auth_user: req.auth_user ?? "",
      enabled: true,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
    connectors = [...connectors, c];
    return { status: "created", connector: c };
  },
  async deleteConnector(name: string) {
    await delay(70);
    connectors = connectors.filter((c) => c.name !== name);
    return { status: "deleted", name };
  },
  async history(limit: number) {
    await delay(80);
    return { history: runHistory.slice(0, limit) };
  },
};

// Keep history list referenced (avoids unused-var lint if tree-shaken differently).
void runHistory;
