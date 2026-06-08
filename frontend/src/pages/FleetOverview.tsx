import { useEffect, useMemo } from "react";
import { Link, useNavigate } from "react-router-dom";
import s from "./pages.module.css";
import f from "./fleet.module.css";
import { StatTile } from "@/components/ui/StatTile";
import { Panel } from "@/components/ui/Panel";
import { TimeSeriesChart } from "@/components/charts/TimeSeriesChart";
import { Badge } from "@/components/ui/Badge";
import { StatusDot } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { EmptyState, Spinner } from "@/components/ui/Misc";
import { useAsync } from "@/hooks/useAsync";
import { useLiveStats } from "@/hooks/useStream";
import { abbrev, int, num, pct } from "@/lib/format";
import { fleetApi } from "@/fleet/fleetApi";
import { useFleetScope } from "@/fleet/scope";
import type { GroupView, NodeView, FleetStats } from "@/fleet/types";
import type { StatsSnapshot } from "@/lib/types";

/* The fleet overview is the landing for fleet scope: cluster-wide tiles, the
   combined throughput chart, a per-group rollup, and the per-node health grid.
   It polls aggregated telemetry (GET /api/fleet/stats) and seeds the live chart
   buffer from /api/fleet/stats/history (design §4 Aggregated telemetry). */

const ZERO: StatsSnapshot = {
  timestamp: 0,
  active_instances: 0,
  total_calls: 0,
  successful_calls: 0,
  failed_calls: 0,
  current_calls: 0,
  calls_per_second: 0,
  avg_response_time_ms: 0,
  success_rate: 100,
};

export function FleetOverview() {
  const navigate = useNavigate();
  const { selectGroup, selectNode } = useFleetScope();

  // Live aggregate via the controller `stats` WS topic (worker shape), seeded
  // from REST history so the chart isn't empty on first paint.
  const { series, latest, seed } = useLiveStats(240);
  const hist = useAsync(() => fleetApi.statsHistory(240), []);
  const stats = useAsync<FleetStats>(() => fleetApi.stats(), [], 2000);
  const nodes = useAsync(() => fleetApi.listNodes(), [], 5000);
  const groups = useAsync(() => fleetApi.listGroups(), [], 5000);

  useEffect(() => {
    if (hist.data?.history?.length) seed(hist.data.history);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hist.data]);

  // Prefer the WS-fed aggregate; fall back to polled fleet stats so the page is
  // alive even if the controller WS hub is down.
  const agg = latest ?? stats.data?.aggregate ?? ZERO;
  const cps = series.map((p) => p.calls_per_second);
  const conc = series.map((p) => p.current_calls);
  const succ = series.map((p) => p.success_rate);

  const nodeList = nodes.data?.nodes ?? [];
  const groupList = groups.data?.groups ?? [];
  const perNode = stats.data?.per_node ?? {};
  const perGroup = stats.data?.per_group ?? {};

  const onlineNodes = useMemo(() => nodeList.filter((n) => n.online), [nodeList]);
  const successRate = agg.success_rate ?? 100;

  return (
    <>
      <div className={s.tiles}>
        <StatTile
          label="Online Nodes"
          value={`${onlineNodes.length}/${nodeList.length}`}
          tone={onlineNodes.length === nodeList.length ? "signal" : "amber"}
          sub={<span className="hud-label">{groupList.length} groups</span>}
        />
        <StatTile
          label="Fleet CPS"
          value={num(agg.calls_per_second ?? 0, 1)}
          unit="cps"
          tone="cyan"
          live
          spark={cps.slice(-40)}
        />
        <StatTile
          label="Total Calls"
          value={abbrev(agg.total_calls ?? 0)}
          tone="signal"
          live
          spark={conc.slice(-40)}
        />
        <StatTile
          label="Success Rate"
          value={num(successRate, 1)}
          unit="%"
          tone={successRate >= 95 ? "signal" : successRate >= 85 ? "amber" : "crit"}
          spark={succ.slice(-40)}
        />
      </div>

      <div className={s.split}>
        <Panel
          title="Combined Throughput"
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
                { label: "Fleet calls/sec", color: "var(--signal)", values: cps, axis: "left" },
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

        <Panel title="Fleet Health" live>
          <dl className={s.kv} style={{ width: "100%" }}>
            <dt>Active tests</dt>
            <dd>{int(agg.active_instances)}</dd>
            <dt>Concurrent calls</dt>
            <dd>{int(agg.current_calls)}</dd>
            <dt>Successful</dt>
            <dd style={{ color: "var(--signal)" }}>{int(agg.successful_calls)}</dd>
            <dt>Failed</dt>
            <dd style={{ color: (agg.failed_calls ?? 0) > 0 ? "var(--crit)" : undefined }}>
              {int(agg.failed_calls)}
            </dd>
            <dt>Avg response</dt>
            <dd>{num(agg.avg_response_time_ms ?? 0, 0)} ms</dd>
          </dl>
          <div style={{ marginTop: "var(--space-4)", display: "flex", gap: "var(--space-2)" }}>
            <Link to="/groups" style={{ flex: 1 }}>
              <Button variant="primary" size="sm" style={{ width: "100%" }}>
                Launch on group
              </Button>
            </Link>
          </div>
        </Panel>
      </div>

      <Panel
        title="Group Rollup"
        actions={
          <Link to="/groups">
            <Button size="sm" variant="ghost">
              Manage groups
            </Button>
          </Link>
        }
      >
        {groups.loading && !groups.data ? (
          <div style={{ display: "grid", placeItems: "center", padding: "var(--space-5)" }}>
            <Spinner />
          </div>
        ) : groupList.length === 0 ? (
          <EmptyState mark="//" title="No groups defined" hint="Create a group to roll up node telemetry." />
        ) : (
          <div className={f.groupRollup}>
            {groupList.map((g) => (
              <GroupRollupCard
                key={g.id}
                group={g}
                snap={perGroup[g.id] ?? ZERO}
                onOpen={() => {
                  selectGroup(g.id);
                  navigate("/groups");
                }}
              />
            ))}
          </div>
        )}
      </Panel>

      <Panel
        title="Node Grid"
        actions={
          <span className="hud-label">
            {onlineNodes.length} online · {nodeList.length} total
          </span>
        }
      >
        {nodes.loading && !nodes.data ? (
          <div style={{ display: "grid", placeItems: "center", padding: "var(--space-5)" }}>
            <Spinner />
          </div>
        ) : nodeList.length === 0 ? (
          <EmptyState
            mark="○"
            title="No nodes registered"
            hint="Add worker nodes to start aggregating fleet telemetry."
            action={
              <Link to="/nodes">
                <Button variant="primary" size="sm">
                  Add node
                </Button>
              </Link>
            }
          />
        ) : (
          <div className={f.nodeGrid}>
            {nodeList.map((n) => (
              <NodeHealthCard
                key={n.id}
                node={n}
                snap={perNode[n.id] ?? null}
                onOpen={() => {
                  selectNode(n.id);
                  navigate("/nodes");
                }}
              />
            ))}
          </div>
        )}
      </Panel>
    </>
  );
}

function GroupRollupCard({
  group,
  snap,
  onOpen,
}: {
  group: GroupView;
  snap: StatsSnapshot;
  onOpen: () => void;
}) {
  return (
    <button type="button" className={f.rollupCard} onClick={onOpen}>
      <div className={f.rollupHead}>
        <span className={f.rollupName}>{group.name}</span>
        <Badge tone={group.online_count === group.total_count ? "signal" : "amber"}>
          {group.online_count}/{group.total_count} up
        </Badge>
      </div>
      <div className={f.rollupMetrics}>
        <Metric label="CPS" value={num(snap.calls_per_second ?? 0, 1)} />
        <Metric label="Calls" value={abbrev(snap.total_calls ?? 0)} />
        <Metric label="Success" value={pct(snap.success_rate ?? 100)} />
      </div>
    </button>
  );
}

function NodeHealthCard({
  node,
  snap,
  onOpen,
}: {
  node: NodeView;
  snap: StatsSnapshot | null;
  onOpen: () => void;
}) {
  const tone = !node.enabled ? "muted" : node.online ? "signal" : "crit";
  const accent = !node.enabled
    ? "var(--text-faint)"
    : node.online
      ? "var(--signal)"
      : "var(--crit)";
  return (
    <button
      type="button"
      className={f.nodeCard}
      style={{ ["--node-accent" as string]: accent }}
      onClick={onOpen}
      title="Drill into this node"
    >
      <div className={f.nodeCardHead}>
        <span className={f.nodeName}>{node.name}</span>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          <StatusDot tone={tone} pulse={node.online && node.active_tests > 0} />
          <span className="hud-label" style={{ color: accent }}>
            {!node.enabled ? "off" : node.online ? "online" : "offline"}
          </span>
        </span>
      </div>
      <div className={f.nodeAddr}>{node.address}</div>
      <div className={f.nodeMetrics}>
        <div className={f.nodeMetric}>
          <span className={f.nodeMetricVal}>{snap ? num(snap.calls_per_second, 0) : "—"}</span>
          <span className={f.nodeMetricLabel}>cps</span>
        </div>
        <div className={f.nodeMetric}>
          <span className={f.nodeMetricVal}>{snap ? abbrev(snap.total_calls) : "—"}</span>
          <span className={f.nodeMetricLabel}>calls</span>
        </div>
        <div className={f.nodeMetric}>
          <span
            className={f.nodeMetricVal}
            style={{
              color: snap && snap.success_rate < 95 ? "var(--amber)" : undefined,
            }}
          >
            {snap ? pct(snap.success_rate) : "—"}
          </span>
          <span className={f.nodeMetricLabel}>ok</span>
        </div>
      </div>
    </button>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className={f.nodeMetric}>
      <span className={f.nodeMetricVal}>{value}</span>
      <span className={f.nodeMetricLabel}>{label}</span>
    </div>
  );
}
