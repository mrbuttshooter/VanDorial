import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Panel } from "@/components/ui/Panel";
import { StatTile } from "@/components/ui/StatTile";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { EmptyState, Spinner } from "@/components/ui/Misc";
import uiStyles from "@/components/ui/ui.module.css";
import { useAsync } from "@/hooks/useAsync";
import { useStream } from "@/hooks/useStream";
import { api } from "@/lib/api";
import { int, num, pct } from "@/lib/format";
import type { LoopStats } from "@/lib/types";

const mins = (ms: number | null | undefined) => (ms ? ms / 60000 : 0);

export function Overview() {
  const loops = useAsync(() => api.listLoops(), [], 3000);
  const servers = useAsync(() => api.listServers(), [], 8000);
  const groups = useAsync(() => api.listNodeGroups(), [], 8000);

  const [stats, setStats] = useState<Record<string, LoopStats>>({});
  useStream<LoopStats>("loops", (st) => {
    if (st?.campaign_id) setStats((p) => ({ ...p, [st.campaign_id]: st }));
  });

  const running = useMemo(
    () => (loops.data?.campaigns ?? []).filter((c) => c.status === "running"),
    [loops.data],
  );

  const totals = useMemo(() => {
    let out = 0, inn = 0, comp = 0, n = 0;
    for (const c of running) {
      const st = stats[c.id];
      if (!st) continue;
      out += mins(st.minutes_out_ms);
      inn += mins(st.minutes_in_ms);
      comp += st.completion_pct;
      n += 1;
    }
    return { out, inn, completion: n ? comp / n : 100 };
  }, [running, stats]);

  return (
    <>
      <div style={{ marginBottom: "var(--space-4)" }}>
        <h1 style={{ margin: 0, color: "var(--text-bright)" }}>Overview</h1>
        <p style={{ margin: "4px 0 0", color: "var(--text-muted)", fontSize: "var(--fs-sm)" }}>
          {int(servers.data?.servers?.length)} nodes · {int(groups.data?.groups?.length)} routes ·{" "}
          {running.length} loops running
        </p>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: "var(--space-3)", marginBottom: "var(--space-5)" }}>
        <StatTile label="Minutes out" value={num(totals.out, 0)} tone="signal" />
        <StatTile label="Minutes in" value={num(totals.inn, 0)} tone="cyan" />
        <StatTile label="Completion" value={num(totals.completion, 1)} unit="%" tone={totals.completion >= 95 ? "signal" : "amber"} />
        <StatTile label="Active loops" value={int(running.length)} tone="cyan" live />
      </div>

      <Panel title="Running loops" flush>
        {loops.loading && !loops.data ? (
          <div style={{ padding: "var(--space-6)", display: "grid", placeItems: "center" }}><Spinner /></div>
        ) : running.length === 0 ? (
          <EmptyState mark="○" title="No loops running" hint="Start a route to drive traffic."
            action={<Link to="/next/routes"><Button variant="primary" size="sm">Go to Routes</Button></Link>} />
        ) : (
          <div className={uiStyles.tableWrap}>
            <table className={uiStyles.table}>
              <thead><tr><th>Campaign</th><th>Source IP → MADA</th><th className={uiStyles.numCell}>min out</th><th className={uiStyles.numCell}>min in</th><th className={uiStyles.numCell}>compl</th><th>State</th></tr></thead>
              <tbody>
                {running.map((c) => {
                  const st = stats[c.id];
                  return (
                    <tr key={c.id}>
                      <td style={{ color: "var(--text-bright)" }}>{c.name}</td>
                      <td style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono, monospace)" }}>
                        {c.local_ip || "auto"} → {c.dest_host}:{c.dest_port}
                      </td>
                      <td className={uiStyles.numCell}>{num(mins(st?.minutes_out_ms), 0)}</td>
                      <td className={uiStyles.numCell}>{num(mins(st?.minutes_in_ms), 0)}</td>
                      <td className={uiStyles.numCell}>{st ? pct(st.completion_pct) : "—"}</td>
                      <td><Badge tone="signal" pulse>running</Badge></td>
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
