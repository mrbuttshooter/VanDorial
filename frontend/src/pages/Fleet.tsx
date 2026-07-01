import { useMemo } from "react";
import s from "./pages.module.css";
import ui from "@/components/ui/ui.module.css";
import { Panel } from "@/components/ui/Panel";
import { StatTile } from "@/components/ui/StatTile";
import { Badge } from "@/components/ui/Badge";
import { EmptyState, Spinner } from "@/components/ui/Misc";
import { useAsync } from "@/hooks/useAsync";
import { api } from "@/lib/api";
import { int, num } from "@/lib/format";
import type { FleetNodeResource, LoopCampaign } from "@/lib/types";

/* Fleet = one row per node (source IP) showing how many loops it's running and
   how much of its box it's burning — CPU and RAM only. Resources come from
   /api/fleet/resources (each remote worker polled at its api_url); loops-running
   is derived from /api/loops/fleet, grouped by source IP. */

/* High utilisation is bad, so the scale runs green → amber → red. */
function utilColor(p: number | null | undefined): string {
  if (p == null) return "var(--text-muted)";
  if (p >= 85) return "var(--crit)";
  if (p >= 60) return "var(--amber)";
  return "var(--signal)";
}

/* A thin horizontal utilisation bar (CPU% / RAM%). */
function Meter({ value }: { value: number | null | undefined }) {
  const w = value == null ? 0 : Math.max(0, Math.min(100, value));
  return (
    <div
      style={{
        height: 6,
        borderRadius: 3,
        background: "var(--bg-inset)",
        overflow: "hidden",
        minWidth: 72,
      }}
    >
      <div style={{ height: "100%", width: `${w}%`, background: utilColor(value) }} />
    </div>
  );
}

function gb(mb: number | null | undefined): string {
  return mb == null ? "—" : num(mb / 1024, 1);
}

export function Fleet() {
  const res = useAsync(() => api.listFleetResources(), [], 3000);
  const loops = useAsync(() => api.listLoopsFleet(), [], 3000);

  const nodes: FleetNodeResource[] = useMemo(() => res.data?.nodes ?? [], [res.data]);
  const campaigns: LoopCampaign[] = useMemo(() => loops.data?.campaigns ?? [], [loops.data]);

  // ip -> count of running loops on it (the per-node "Loops running" column).
  const runningByIp = useMemo(() => {
    const m: Record<string, number> = {};
    for (const c of campaigns) {
      if (c.status !== "running") continue;
      const ip = c.local_ip ?? "";
      m[ip] = (m[ip] ?? 0) + 1;
    }
    return m;
  }, [campaigns]);

  const online = nodes.filter((n) => n.online).length;
  const runningTotal = campaigns.filter((c) => c.status === "running").length;
  const peakCpu = nodes.reduce((mx, n) => Math.max(mx, n.cpu_percent ?? 0), 0);
  const peakMem = nodes.reduce((mx, n) => Math.max(mx, n.mem_percent ?? 0), 0);

  const loading = res.loading && !res.data;

  return (
    <>
      {/* ---- Summary tiles ---- */}
      <div className={s.tiles}>
        <StatTile
          label="Nodes online"
          value={int(online)}
          tone={online === nodes.length ? "signal" : "amber"}
          sub={<span className="hud-label">{nodes.length} total</span>}
        />
        <StatTile label="Loops running" value={int(runningTotal)} tone="cyan" />
        <StatTile
          label="Peak CPU"
          value={num(peakCpu, 0)}
          unit="%"
          tone={peakCpu >= 85 ? "crit" : peakCpu >= 60 ? "amber" : "signal"}
        />
        <StatTile
          label="Peak RAM"
          value={num(peakMem, 0)}
          unit="%"
          tone={peakMem >= 85 ? "crit" : peakMem >= 60 ? "amber" : "signal"}
        />
      </div>

      {/* ---- Per-node table ---- */}
      <Panel title="Fleet nodes" flush live>
        {loading ? (
          <div style={{ padding: "var(--space-6)", display: "grid", placeItems: "center" }}>
            <Spinner />
          </div>
        ) : res.error && !res.data ? (
          <EmptyState
            mark="⚠"
            title="Controller unreachable"
            hint={res.error}
          />
        ) : nodes.length === 0 ? (
          <EmptyState
            title="No nodes"
            hint="Add a node on the Nodes page (give it a worker URL + key to run on a remote box)."
          />
        ) : (
          <div className={ui.tableWrap}>
            <table className={ui.table}>
              <thead>
                <tr>
                  <th>Node</th>
                  <th>Source IP</th>
                  <th className={ui.numCell}>Loops</th>
                  <th>CPU</th>
                  <th>RAM</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {nodes.map((n) => {
                  const ip = n.ip ?? "";
                  const cpu = n.cpu_percent;
                  const memUsed = n.mem_used_mb;
                  const memTotal = n.mem_total_mb;
                  return (
                    <tr key={`${n.box}:${n.id ?? ip}`}>
                      <td style={{ color: "var(--text-bright)", fontWeight: 600 }}>
                        {n.name || n.hostname || "—"}
                      </td>
                      <td
                        style={{
                          color: "var(--text-muted)",
                          fontFamily: "var(--font-mono, monospace)",
                        }}
                      >
                        {ip || "—"}
                        {n.remote ? <span style={{ color: "var(--cyan)" }}> ⇄</span> : null}
                      </td>
                      <td className={ui.numCell} style={{ color: "var(--text-bright)" }}>
                        {int(runningByIp[ip] ?? 0)}
                      </td>
                      {/* CPU: bar + % (and load vs cores when known) */}
                      <td>
                        {n.online ? (
                          <div style={{ display: "grid", gap: 4, minWidth: 120 }}>
                            <Meter value={cpu} />
                            <span className="hud-label" style={{ color: utilColor(cpu) }}>
                              {cpu == null ? "—" : `${num(cpu, 0)}%`}
                              {n.load1 != null && n.cores
                                ? `  ·  load ${num(n.load1, 2)}/${n.cores}`
                                : ""}
                            </span>
                          </div>
                        ) : (
                          <span style={{ color: "var(--text-muted)" }}>—</span>
                        )}
                      </td>
                      {/* RAM: bar + used/total GB + % */}
                      <td>
                        {n.online ? (
                          <div style={{ display: "grid", gap: 4, minWidth: 120 }}>
                            <Meter value={n.mem_percent} />
                            <span className="hud-label" style={{ color: utilColor(n.mem_percent) }}>
                              {gb(memUsed)} / {gb(memTotal)} GB
                              {n.mem_percent != null ? `  ·  ${num(n.mem_percent, 0)}%` : ""}
                            </span>
                          </div>
                        ) : (
                          <span style={{ color: "var(--text-muted)" }}>—</span>
                        )}
                      </td>
                      <td>
                        {n.online ? (
                          <Badge tone="signal">online</Badge>
                        ) : (
                          <span title={n.error ?? undefined}>
                            <Badge tone="crit">offline</Badge>
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </>
  );
}
