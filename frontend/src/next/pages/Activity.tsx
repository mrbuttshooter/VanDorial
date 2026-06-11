import { Panel } from "@/components/ui/Panel";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { EmptyState, Spinner } from "@/components/ui/Misc";
import { IconDownload } from "@/components/icons";
import { statusTone } from "@/components/ui/tone";
import uiStyles from "@/components/ui/ui.module.css";
import { useAsync } from "@/hooks/useAsync";
import { api } from "@/lib/api";
import { useToast } from "@/components/ui/Toast";
import { ago } from "@/lib/format";

export function Activity() {
  const loops = useAsync(() => api.listLoops(), [], 3000);
  const toast = useToast();
  const rows = loops.data?.campaigns ?? [];

  const download = async (id: string) => {
    try {
      await api.downloadLoopRecordsCsv(id);
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    }
  };

  return (
    <>
      <div style={{ marginBottom: "var(--space-4)" }}>
        <h1 style={{ margin: 0, color: "var(--text-bright)" }}>Activity</h1>
        <p style={{ margin: "4px 0 0", color: "var(--text-muted)", fontSize: "var(--fs-sm)" }}>
          Loop campaigns — minutes accounting + per-call RFC delta. Download records.
        </p>
      </div>
      <Panel title="Campaigns" flush>
        {loops.loading && !loops.data ? (
          <div style={{ padding: "var(--space-6)", display: "grid", placeItems: "center" }}><Spinner /></div>
        ) : rows.length === 0 ? (
          <EmptyState mark="○" title="No campaigns yet" hint="Start a route to create loop campaigns." />
        ) : (
          <div className={uiStyles.tableWrap}>
            <table className={uiStyles.table}>
              <thead><tr><th>Campaign</th><th>Source IP → MADA</th><th>State</th><th>Started</th><th></th></tr></thead>
              <tbody>
                {rows.map((c) => (
                  <tr key={c.id}>
                    <td style={{ color: "var(--text-bright)" }}>{c.name}</td>
                    <td style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono, monospace)" }}>
                      {c.local_ip || "auto"} → {c.dest_host}:{c.dest_port}
                    </td>
                    <td><Badge tone={statusTone(c.status)} pulse={c.status === "running"}>{c.status}</Badge></td>
                    <td style={{ color: "var(--text-muted)" }}>{ago(c.started_at)}</td>
                    <td style={{ textAlign: "right" }}>
                      <Button size="sm" variant="ghost" icon title="Download records CSV" onClick={() => download(c.id)}>
                        <IconDownload />
                      </Button>
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
