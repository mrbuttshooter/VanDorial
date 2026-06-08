import s from "./pages.module.css";
import { Panel } from "@/components/ui/Panel";
import { Badge } from "@/components/ui/Badge";
import { Spinner } from "@/components/ui/Misc";
import { useAsync } from "@/hooks/useAsync";
import { api } from "@/lib/api";
import { MOCK_ENABLED } from "@/lib/mock";

export function Config() {
  const health = useAsync(() => api.health(), [], 5000);

  return (
    <div className={s.split}>
      <Panel title="Server">
        {health.loading && !health.data ? (
          <div style={{ display: "grid", placeItems: "center", padding: "var(--space-5)" }}>
            <Spinner />
          </div>
        ) : (
          <dl className={s.kv}>
            <dt>Service</dt>
            <dd>{health.data?.name ?? "GenCall"}</dd>
            <dt>Version</dt>
            <dd>{health.data?.version ?? "—"}</dd>
            <dt>Status</dt>
            <dd>
              <Badge tone={health.data?.status === "ok" ? "signal" : "crit"} pulse>
                {health.data?.status ?? "unknown"}
              </Badge>
            </dd>
            <dt>Active tests</dt>
            <dd>{health.data?.active_tests ?? 0}</dd>
            <dt>Data source</dt>
            <dd>
              <Badge tone={MOCK_ENABLED ? "amber" : "cyan"}>
                {MOCK_ENABLED ? "mock simulator" : "live backend"}
              </Badge>
            </dd>
          </dl>
        )}
      </Panel>

      <Panel title="Runtime">
        <dl className={s.kv}>
          <dt>Console build</dt>
          <dd>2.1.0</dd>
          <dt>Stream transport</dt>
          <dd>WebSocket /ws</dd>
          <dt>API base</dt>
          <dd>/api</dd>
          <dt>Stats interval</dt>
          <dd>1s</dd>
        </dl>
        <p style={{ marginTop: "var(--space-4)", fontSize: "var(--fs-sm)", color: "var(--text-muted)", lineHeight: 1.6 }}>
          Server-side configuration lives in <code>gencall/etc/gencall.cfg</code>. SIP timers, RTP
          port range, database engine and SIPp binary path are set there and applied on restart.
        </p>
      </Panel>
    </div>
  );
}
