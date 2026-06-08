import { describe, expect, it } from "vitest";
import { fleetApi } from "./fleetApi";

/* These run against the in-browser fleet mock (FLEET_MOCK_ENABLED is true under
   vitest's DEV env), which is exactly what the fleet console renders against in
   demo mode. They lock the launch contract the Groups "Launch on group" modal
   depends on: a group launch fans out to that group's online nodes and reports
   per-node dispatch results (design §4/§5). */

describe("fleetApi (mock) — launch on group", () => {
  it("lists seeded groups and nodes", async () => {
    const { groups } = await fleetApi.listGroups();
    const { nodes } = await fleetApi.listNodes();
    expect(groups.length).toBeGreaterThan(0);
    expect(nodes.length).toBeGreaterThan(0);
    // GroupView rollup counts are well-formed.
    for (const g of groups) {
      expect(g.online_count).toBeLessThanOrEqual(g.total_count);
    }
  });

  it("fans out a per_node launch to a group's online nodes", async () => {
    const { groups } = await fleetApi.listGroups();
    const target = groups.find((g) => g.online_count > 0);
    expect(target).toBeDefined();

    const res = await fleetApi.launch({
      group_id: target!.id,
      scenario: "basic_call",
      destination: { remote_host: "10.20.8.40", remote_port: 5060, transport: "udp" },
      rate: { mode: "per_node", value: 25 },
    });

    expect(res.fleet_run_id).toBeGreaterThan(0);
    expect(res.dispatched.length).toBe(target!.total_count);
    const ok = res.dispatched.filter((d) => d.ok);
    expect(ok.length).toBe(target!.online_count);
    // Successful dispatches carry a worker test id.
    for (const d of ok) expect(d.test_id).toBeTruthy();
  });

  it("records the run so the run list reflects it", async () => {
    const { groups } = await fleetApi.listGroups();
    const target = groups.find((g) => g.online_count > 0)!;
    const res = await fleetApi.launch({
      group_id: target.id,
      scenario: "basic_call",
      destination: { remote_host: "10.20.8.41" },
      rate: { mode: "total", value: 100 },
    });
    const { runs } = await fleetApi.listRuns(50);
    const recorded = runs.find((r) => r.id === res.fleet_run_id);
    expect(recorded).toBeDefined();
    expect(recorded!.rate_mode).toBe("total");
    expect(recorded!.rate_value).toBe(100);
  });
});
