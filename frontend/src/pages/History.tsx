import { useMemo, useState } from "react";
import s from "./pages.module.css";
import ui from "@/components/ui/ui.module.css";
import { Panel } from "@/components/ui/Panel";
import { Badge, statusTone } from "@/components/ui/Badge";
import { EmptyState, Spinner } from "@/components/ui/Misc";
import { useAsync } from "@/hooks/useAsync";
import { api } from "@/lib/api";
import { datetime, duration, int, ms, pct } from "@/lib/format";
import type { RunStatus } from "@/lib/types";

const FILTERS: ("all" | RunStatus)[] = ["all", "completed", "failed", "stopped"];

export function History() {
  const hist = useAsync(() => api.history(100), []);
  const [filter, setFilter] = useState<"all" | RunStatus>("all");

  const rows = useMemo(() => {
    const all = hist.data?.history ?? [];
    return filter === "all" ? all : all.filter((r) => r.status === filter);
  }, [hist.data, filter]);

  return (
    <>
      <div className={s.toolbar}>
        <div className={s.seg}>
          {FILTERS.map((f) => (
            <button
              key={f}
              className={`${s.segBtn} ${filter === f ? s.segActive : ""}`}
              onClick={() => setFilter(f)}
            >
              {f}
            </button>
          ))}
        </div>
        <div className={s.spacer} />
        <span className="hud-label">{rows.length} runs</span>
      </div>

      <Panel title="Run Archive" flush>
        {hist.loading && !hist.data ? (
          <div style={{ padding: "var(--space-6)", display: "grid", placeItems: "center" }}>
            <Spinner />
          </div>
        ) : rows.length === 0 ? (
          <EmptyState title="No runs recorded" hint="Completed campaigns will be archived here." />
        ) : (
          <div className={ui.tableWrap}>
            <table className={ui.table}>
              <thead>
                <tr>
                  <th>Run</th>
                  <th>Scenario</th>
                  <th>Connector</th>
                  <th className={ui.numCell}>Calls</th>
                  <th className={ui.numCell}>ASR</th>
                  <th className={ui.numCell}>Avg RT</th>
                  <th className={ui.numCell}>Duration</th>
                  <th>Status</th>
                  <th>Started</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const asr = r.total_calls ? (r.successful_calls / r.total_calls) * 100 : 0;
                  return (
                    <tr key={r.id}>
                      <td style={{ color: "var(--text-bright)", fontWeight: 600 }}>{r.name}</td>
                      <td style={{ color: "var(--text-muted)" }}>{r.scenario_name || "—"}</td>
                      <td style={{ color: "var(--text-muted)" }}>{r.connector_name || "—"}</td>
                      <td className={ui.numCell}>{int(r.total_calls)}</td>
                      <td
                        className={ui.numCell}
                        style={{ color: asr >= 95 ? "var(--signal)" : asr >= 85 ? "var(--amber)" : "var(--crit)" }}
                      >
                        {pct(asr)}
                      </td>
                      <td className={ui.numCell}>{ms(r.avg_response_time_ms)}</td>
                      <td className={ui.numCell}>{duration(r.duration)}</td>
                      <td>
                        <Badge tone={statusTone(r.status)}>{r.status}</Badge>
                      </td>
                      <td style={{ color: "var(--text-muted)" }}>{datetime(r.started_at)}</td>
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
