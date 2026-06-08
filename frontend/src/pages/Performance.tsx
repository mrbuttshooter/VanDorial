import { useEffect, useMemo } from "react";
import s from "./pages.module.css";
import { Panel } from "@/components/ui/Panel";
import { StatTile } from "@/components/ui/StatTile";
import { TimeSeriesChart } from "@/components/charts/TimeSeriesChart";
import { RadialGauge } from "@/components/charts/RadialGauge";
import { Spinner } from "@/components/ui/Misc";
import { useAsync } from "@/hooks/useAsync";
import { useLiveStats } from "@/hooks/useStream";
import { api } from "@/lib/api";
import { int, num } from "@/lib/format";

export function Performance() {
  const { series, latest, seed } = useLiveStats(240);
  const hist = useAsync(() => api.statsHistory(240), []);

  useEffect(() => {
    if (hist.data?.history?.length) seed(hist.data.history);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hist.data]);

  const cps = series.map((p) => p.calls_per_second);
  const rt = series.map((p) => p.avg_response_time_ms);
  const succ = series.map((p) => p.success_rate);
  const conc = series.map((p) => p.current_calls);

  const peaks = useMemo(() => {
    const max = (arr: number[]) => (arr.length ? Math.max(...arr) : 0);
    const avg = (arr: number[]) => (arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : 0);
    return {
      peakCps: max(cps),
      peakConc: max(conc),
      avgRt: avg(rt),
      maxRt: max(rt),
      avgSucc: avg(succ),
    };
  }, [cps, conc, rt, succ]);

  if (series.length < 2 && hist.loading) {
    return (
      <Panel>
        <div style={{ display: "grid", placeItems: "center", padding: "var(--space-7)" }}>
          <Spinner />
        </div>
      </Panel>
    );
  }

  return (
    <>
      <div className={s.tiles}>
        <StatTile label="Peak CPS" value={num(peaks.peakCps, 1)} tone="signal" spark={cps.slice(-40)} />
        <StatTile label="Peak Concurrent" value={int(peaks.peakConc)} tone="cyan" spark={conc.slice(-40)} />
        <StatTile label="Avg Response" value={num(peaks.avgRt, 0)} unit="ms" tone="amber" spark={rt.slice(-40)} />
        <StatTile
          label="Avg Success"
          value={num(peaks.avgSucc, 1)}
          unit="%"
          tone={peaks.avgSucc >= 95 ? "signal" : "amber"}
          spark={succ.slice(-40)}
        />
      </div>

      <Panel title="Throughput vs Latency" live>
        {series.length > 1 ? (
          <TimeSeriesChart
            height={280}
            series={[
              { label: "Calls/sec", color: "var(--signal)", values: cps, axis: "left" },
              { label: "Response (ms)", color: "var(--amber)", values: rt, axis: "right" },
            ]}
            format={(v) => num(v, 0)}
            formatRight={(v) => `${num(v, 0)}`}
          />
        ) : (
          <div style={{ height: 280, display: "grid", placeItems: "center" }}>
            <Spinner />
          </div>
        )}
      </Panel>

      <div className={s.split}>
        <Panel title="Success Rate" live>
          {series.length > 1 ? (
            <TimeSeriesChart
              height={220}
              series={[{ label: "Success %", color: "var(--cyan)", values: succ }]}
              format={(v) => `${num(v, 0)}%`}
            />
          ) : (
            <div style={{ height: 220, display: "grid", placeItems: "center" }}>
              <Spinner />
            </div>
          )}
        </Panel>

        <Panel title="Current Health" live>
          <div className={s.gaugeWrap}>
            <RadialGauge value={latest?.success_rate ?? 100} label="Live success" />
            <dl className={s.kv} style={{ width: "100%" }}>
              <dt>Peak response</dt>
              <dd>{num(peaks.maxRt, 0)} ms</dd>
              <dt>Active instances</dt>
              <dd>{int(latest?.active_instances)}</dd>
              <dt>Samples</dt>
              <dd>{series.length}</dd>
            </dl>
          </div>
        </Panel>
      </div>
    </>
  );
}
