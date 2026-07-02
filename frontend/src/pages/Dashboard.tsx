import { useEffect, useMemo } from "react";
import { Link } from "react-router-dom";
import s from "./pages.module.css";
import { StatTile } from "@/components/ui/StatTile";
import { Panel } from "@/components/ui/Panel";
import { TimeSeriesChart } from "@/components/charts/TimeSeriesChart";
import { RadialGauge } from "@/components/charts/RadialGauge";
import { Badge } from "@/components/ui/Badge";
import { statusTone } from "@/components/ui/tone";
import { Button } from "@/components/ui/Button";
import { EmptyState, Spinner } from "@/components/ui/Misc";
import { useAsync } from "@/hooks/useAsync";
import { useLiveStats } from "@/hooks/useStream";
import { api } from "@/lib/api";
import { abbrev, duration, int, ms, num, pct } from "@/lib/format";
import uiStyles from "@/components/ui/ui.module.css";

export function Dashboard() {
  const { series, latest, seed } = useLiveStats(180);
  const tests = useAsync(() => api.listTests(), [], 3000);
  const hist = useAsync(() => api.statsHistory(180), []);

  // Seed the live buffer once from REST history so charts aren't empty.
  useEffect(() => {
    if (hist.data?.history?.length) seed(hist.data.history);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hist.data]);

  const cps = series.map((p) => p.calls_per_second);
  const conc = series.map((p) => p.current_calls);
  const succ = series.map((p) => p.success_rate);
  const rt = series.map((p) => p.avg_response_time_ms);

  const running = useMemo(
    () => (tests.data?.tests ?? []).filter((t) => t.state === "running"),
    [tests.data],
  );

  // Fleet-wide loops: this box + every remote worker (so the controller's
  // dashboard isn't empty when the loops actually run on other boxes). Polled.
  const loops = useAsync(() => api.listLoopsFleet(), [], 5000);
  const runningLoops = useMemo(
    () => (loops.data?.campaigns ?? []).filter((c) => c.status === "running"),
    [loops.data],
  );
  const loopAgg = useMemo(() => {
    let cps = 0, out = 0, ans = 0;
    for (const c of runningLoops) {
      cps += c.rate ?? 0;
      out += c.loop_stats?.calls_out ?? 0;
      ans += c.loop_stats?.answered_out ?? 0;
    }
    return { count: runningLoops.length, cps, out, ans, asr: out > 0 ? (ans / out) * 100 : 0 };
  }, [runningLoops]);
  const boxLabel = (b?: string) =>
    !b || b === "local" ? "local" : b.replace(/^https?:\/\//, "").replace(/:\d+$/, "");

  const successRate = latest?.success_rate ?? 100;

  return (
    <>
      <div
        style={{
          display: "flex",
          justifyContent: "flex-end",
          gap: "var(--space-3)",
          marginBottom: "var(--space-4)",
        }}
      >
        <Link to="/loops">
          <Button variant="primary">+ New Loop Campaign</Button>
        </Link>
        <Link to="/campaigns">
          <Button variant="ghost">+ New Test</Button>
        </Link>
      </div>

      <div className={s.tiles}>
        <StatTile
          label="Active Tests"
          value={int(latest?.active_instances)}
          tone="signal"
          live
          spark={conc.slice(-40)}
        />
        <StatTile
          label="Calls / sec"
          value={num(latest?.calls_per_second ?? 0, 1)}
          unit="cps"
          tone="cyan"
          live
          spark={cps.slice(-40)}
        />
        <StatTile
          label="Success Rate"
          value={num(successRate, 1)}
          unit="%"
          tone={successRate >= 95 ? "signal" : successRate >= 85 ? "amber" : "crit"}
          spark={succ.slice(-40)}
        />
        <StatTile
          label="Avg Response"
          value={num(latest?.avg_response_time_ms ?? 0, 0)}
          unit="ms"
          tone="amber"
          spark={rt.slice(-40)}
        />
      </div>

      <Panel
        title={`Fleet Loops — Running (${loopAgg.count})`}
        flush
        actions={
          <span className="hud-label">
            {`${num(loopAgg.cps, 1)} cps · ${int(loopAgg.out)} out · ${num(loopAgg.asr, 1)}% ASR`}
          </span>
        }
      >
        {loops.loading && !loops.data ? (
          <div style={{ padding: "var(--space-6)", display: "grid", placeItems: "center" }}>
            <Spinner />
          </div>
        ) : loops.error && !loops.data ? (
          <EmptyState
            mark="⚠"
            title="Controller unreachable"
            hint={loops.error}
          />
        ) : runningLoops.length === 0 ? (
          <EmptyState
            mark="○"
            title="No loops running across the fleet"
            hint="Start a loop campaign on any node; running loops from every box show here."
            action={
              <Link to="/loops">
                <Button variant="primary" size="sm">
                  New loop campaign
                </Button>
              </Link>
            }
          />
        ) : (
          <div className={uiStyles.tableWrap}>
            <table className={uiStyles.table}>
              <thead>
                <tr>
                  <th>Box</th>
                  <th>Campaign</th>
                  <th>Target</th>
                  <th className={uiStyles.numCell}>CPS</th>
                  <th className={uiStyles.numCell}>Calls Out</th>
                  <th className={uiStyles.numCell}>Answered</th>
                  <th className={uiStyles.numCell}>ASR</th>
                  <th>State</th>
                </tr>
              </thead>
              <tbody>
                {runningLoops.map((c) => {
                  const out = c.loop_stats?.calls_out ?? 0;
                  const ans = c.loop_stats?.answered_out ?? 0;
                  const asr = out > 0 ? (ans / out) * 100 : 0;
                  return (
                    <tr key={`${c.box ?? "local"}:${c.id}`}>
                      <td style={{ color: "var(--cyan)" }}>{boxLabel(c.box)}</td>
                      <td style={{ color: "var(--text-bright)", fontWeight: 600 }}>{c.name}</td>
                      <td style={{ color: "var(--text-muted)" }}>
                        {c.dest_host}:{c.dest_port}
                      </td>
                      <td className={uiStyles.numCell}>{num(c.rate, 1)}</td>
                      <td className={uiStyles.numCell}>{int(out)}</td>
                      <td className={uiStyles.numCell}>{int(ans)}</td>
                      <td
                        className={uiStyles.numCell}
                        style={{ color: asr >= 50 ? "var(--ok)" : "var(--amber)" }}
                      >
                        {pct(asr)}
                      </td>
                      <td>
                        <Badge tone={statusTone(c.status)} pulse>
                          {c.status}
                        </Badge>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <div className={s.split}>
        <Panel
          title="Aggregate Throughput"
          live
          actions={
            <span className="hud-label">
              {series.length ? `${series.length} samples` : "warming up"}
            </span>
          }
        >
          {series.length > 1 ? (
            <TimeSeriesChart
              height={260}
              series={[
                { label: "Calls/sec", color: "var(--signal)", values: cps, axis: "left" },
                { label: "Concurrent", color: "var(--cyan)", values: conc, axis: "right" },
              ]}
              format={(v) => num(v, 0)}
              formatRight={(v) => abbrev(v)}
            />
          ) : (
            <div style={{ height: 260, display: "grid", placeItems: "center" }}>
              <Spinner />
            </div>
          )}
        </Panel>

        <Panel title="Call Health" live>
          <div className={s.gaugeWrap}>
            <RadialGauge value={successRate} label="Success" />
            <dl className={s.kv} style={{ width: "100%" }}>
              <dt>Total calls</dt>
              <dd>{int(latest?.total_calls)}</dd>
              <dt>Successful</dt>
              <dd style={{ color: "var(--ok)" }}>{int(latest?.successful_calls)}</dd>
              <dt>Failed</dt>
              <dd style={{ color: (latest?.failed_calls ?? 0) > 0 ? "var(--crit)" : undefined }}>
                {int(latest?.failed_calls)}
              </dd>
              <dt>Concurrent</dt>
              <dd>{int(latest?.current_calls)}</dd>
            </dl>
          </div>
        </Panel>
      </div>

      <Panel
        title="Running Tests"
        flush
        actions={
          <Link to="/campaigns">
            <Button size="sm" variant="ghost">
              View all
            </Button>
          </Link>
        }
      >
        {tests.loading && !tests.data ? (
          <div style={{ padding: "var(--space-6)", display: "grid", placeItems: "center" }}>
            <Spinner />
          </div>
        ) : running.length === 0 ? (
          <EmptyState
            mark="○"
            title="No tests running"
            hint="Launch a campaign to start generating SIP traffic."
            action={
              <Link to="/campaigns">
                <Button variant="primary" size="sm">
                  New campaign
                </Button>
              </Link>
            }
          />
        ) : (
          <div className={uiStyles.tableWrap}>
            <table className={uiStyles.table}>
              <thead>
                <tr>
                  <th>Test</th>
                  <th>Target</th>
                  <th>Transport</th>
                  <th className={uiStyles.numCell}>CPS</th>
                  <th className={uiStyles.numCell}>Calls</th>
                  <th className={uiStyles.numCell}>Success</th>
                  <th className={uiStyles.numCell}>Uptime</th>
                  <th>State</th>
                </tr>
              </thead>
              <tbody>
                {running.map((t) => (
                  <tr key={t.id}>
                    <td style={{ color: "var(--text-bright)", fontWeight: 600 }}>{t.id}</td>
                    <td style={{ color: "var(--text-muted)" }}>
                      {t.remote_host}:{t.remote_port}
                    </td>
                    <td style={{ textTransform: "uppercase" }}>{t.transport}</td>
                    <td className={uiStyles.numCell}>{num(t.stats.calls_per_second, 1)}</td>
                    <td className={uiStyles.numCell}>{int(t.stats.total_calls)}</td>
                    <td
                      className={uiStyles.numCell}
                      style={{
                        color:
                          t.stats.success_rate >= 95 ? "var(--ok)" : "var(--amber)",
                      }}
                    >
                      {pct(t.stats.success_rate)}
                    </td>
                    <td className={uiStyles.numCell} title={`${ms(t.stats.avg_response_time_ms)} avg`}>
                      {duration(t.stats.uptime_seconds)}
                    </td>
                    <td>
                      <Badge tone={statusTone(t.state)} pulse={t.state === "running"}>
                        {t.state}
                      </Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </>
  );
}
