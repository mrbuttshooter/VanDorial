import { useMemo, useState } from "react";
import s from "./pages.module.css";
import ui from "@/components/ui/ui.module.css";
import { Panel } from "@/components/ui/Panel";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { statusTone } from "@/components/ui/tone";
import { EmptyState, Spinner } from "@/components/ui/Misc";
import { IconDownload } from "@/components/icons";
import { useAsync } from "@/hooks/useAsync";
import { api } from "@/lib/api";
import { useToast } from "@/components/ui/Toast";
import { datetime, duration, int, ms, num, pct } from "@/lib/format";
import type { LoopCampaign, RunStatus } from "@/lib/types";

const TEST_FILTERS: ("all" | RunStatus)[] = ["all", "completed", "failed", "stopped"];

/** ms → minutes (1 dp). */
function mins(x: number | null | undefined): number {
  return x == null || Number.isNaN(x) ? 0 : x / 60000;
}

export function History() {
  const [tab, setTab] = useState<"loops" | "tests">("loops");

  return (
    <>
      <div className={s.toolbar}>
        <div className={s.seg}>
          <button
            className={`${s.segBtn} ${tab === "loops" ? s.segActive : ""}`}
            onClick={() => setTab("loops")}
          >
            Loops
          </button>
          <button
            className={`${s.segBtn} ${tab === "tests" ? s.segActive : ""}`}
            onClick={() => setTab("tests")}
          >
            Tests
          </button>
        </div>
      </div>
      {tab === "loops" ? <LoopHistory /> : <TestHistory />}
    </>
  );
}

/* ---- Ran loops --------------------------------------------------------------
   Every loop campaign (a "run"), newest first, with its final accounting. */
function LoopHistory() {
  const hist = useAsync(() => api.listLoopsFleet(), [], 5000);
  const toast = useToast();

  const runs: LoopCampaign[] = hist.data?.campaigns ?? [];

  const download = async (id: string) => {
    try {
      await api.downloadLoopRecordsCsv(id);
    } catch (e) {
      toast.error(`Download failed: ${e instanceof Error ? e.message : e}`);
    }
  };

  return (
    <Panel title="Loop runs" flush>
      {hist.loading && !hist.data ? (
        <div style={{ padding: "var(--space-6)", display: "grid", placeItems: "center" }}>
          <Spinner />
        </div>
      ) : runs.length === 0 ? (
        <EmptyState title="No loop runs yet" hint="Run a saved loop and it will be archived here." />
      ) : (
        <div className={ui.tableWrap}>
          <table className={ui.table}>
            <thead>
              <tr>
                <th>Run</th>
                <th>Source → Destination</th>
                <th className={ui.numCell}>Calls (out/ans)</th>
                <th className={ui.numCell}>ASR</th>
                <th className={ui.numCell}>ACD</th>
                <th className={ui.numCell}>Completion</th>
                <th className={ui.numCell}>Min out/in</th>
                <th>Status</th>
                <th>Started</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => {
                const st = r.loop_stats ?? undefined;
                const callsOut = st?.calls_out ?? 0;
                const answered = st?.answered_out ?? 0;
                const asr = callsOut ? (answered / callsOut) * 100 : 0;
                const acdS = answered ? (st!.minutes_out_ms / answered) / 1000 : 0;
                return (
                  <tr key={r.id}>
                    <td style={{ color: "var(--text-bright)", fontWeight: 600 }}>{r.name}</td>
                    <td style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono, monospace)" }}>
                      {r.local_ip ? `${r.local_ip} → ` : ""}
                      {r.dest_host}:{r.dest_port}
                      {r.box && r.box !== "local" ? (
                        <span style={{ color: "var(--cyan)" }}> ⇄ {r.box.replace(/^https?:\/\//, "")}</span>
                      ) : null}
                    </td>
                    <td className={ui.numCell}>
                      {int(callsOut)} / {int(answered)}
                    </td>
                    <td
                      className={ui.numCell}
                      style={{ color: asr >= 95 ? "var(--signal)" : asr >= 50 ? "var(--amber)" : "var(--crit)" }}
                    >
                      {st ? pct(asr) : "—"}
                    </td>
                    <td className={ui.numCell}>{st ? duration(acdS) : "—"}</td>
                    <td className={ui.numCell}>{st ? pct(st.completion_pct) : "—"}</td>
                    <td className={ui.numCell}>
                      {num(mins(st?.minutes_out_ms))} / {num(mins(st?.minutes_in_ms))}
                    </td>
                    <td>
                      <Badge tone={statusTone(r.status)}>{r.status}</Badge>
                    </td>
                    <td style={{ color: "var(--text-muted)" }}>{datetime(r.started_at)}</td>
                    <td style={{ textAlign: "right" }}>
                      <Button size="sm" variant="ghost" title="Download records CSV" onClick={() => download(r.id)}>
                        <IconDownload />
                      </Button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

/* ---- One-shot test runs (the original archive) ---------------------------- */
function TestHistory() {
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
          {TEST_FILTERS.map((f) => (
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

      <Panel title="Test run archive" flush>
        {hist.loading && !hist.data ? (
          <div style={{ padding: "var(--space-6)", display: "grid", placeItems: "center" }}>
            <Spinner />
          </div>
        ) : rows.length === 0 ? (
          <EmptyState title="No runs recorded" hint="Completed one-shot tests will be archived here." />
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
