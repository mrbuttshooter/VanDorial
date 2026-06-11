import { useMemo } from "react";
import { Link } from "react-router-dom";
import { Panel } from "@/components/ui/Panel";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { EmptyState, Spinner } from "@/components/ui/Misc";
import uiStyles from "@/components/ui/ui.module.css";
import { useAsync } from "@/hooks/useAsync";
import { api } from "@/lib/api";
import { int } from "@/lib/format";

/**
 * Servers: the boxes in the fleet. On a single worker this shows THIS box and
 * its source-IP nodes (number pools). Drilling into other discovered boxes is
 * the controller-proxy phase; here we surface the local box's nodes so the
 * structure is visible. Node management still lives on the classic Nodes page.
 */
export function Servers() {
  const nodes = useAsync(() => api.listServers(), [], 4000);
  const groups = useAsync(() => api.listNodeGroups(), [], 8000);

  const groupName = useMemo(() => {
    const m = new Map<number, string>();
    (groups.data?.groups ?? []).forEach((g) => m.set(g.id, g.name));
    return m;
  }, [groups.data]);

  const rows = nodes.data?.servers ?? [];

  return (
    <>
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", marginBottom: "var(--space-4)" }}>
        <div>
          <h1 style={{ margin: 0, color: "var(--text-bright)" }}>Servers</h1>
          <p style={{ margin: "4px 0 0", color: "var(--text-muted)", fontSize: "var(--fs-sm)" }}>
            Boxes in the VLAN and their source-IP nodes. One loop per IP.
          </p>
        </div>
        <Link to="/nodes"><Button variant="primary" size="sm">Manage nodes</Button></Link>
      </div>

      <Panel
        title={<>This server <Badge tone="signal">{int(rows.length)} nodes</Badge></>}
        flush
      >
        {nodes.loading && !nodes.data ? (
          <div style={{ padding: "var(--space-6)", display: "grid", placeItems: "center" }}><Spinner /></div>
        ) : rows.length === 0 ? (
          <EmptyState mark="○" title="No source IPs yet"
            hint="Add a node (source IP + origin/drop zone) so it can run a loop."
            action={<Link to="/nodes"><Button variant="primary" size="sm">Add node</Button></Link>} />
        ) : (
          <div className={uiStyles.tableWrap}>
            <table className={uiStyles.table}>
              <thead><tr><th>Node</th><th>Source IP</th><th>Origin → Drop zone</th><th className={uiStyles.numCell}>Pool</th><th>Route</th></tr></thead>
              <tbody>
                {rows.map((n) => (
                  <tr key={n.id}>
                    <td style={{ color: "var(--text-bright)" }}>{n.name}</td>
                    <td style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono, monospace)" }}>{n.ip}</td>
                    <td style={{ color: "var(--text-muted)" }}>
                      {n.has_pool ? `${n.origin_zone} → ${n.dest_zone}` : "— no pool —"}
                    </td>
                    <td className={uiStyles.numCell}>
                      {n.has_pool ? <Badge tone="signal">{int(n.pool_count)}</Badge> : <Badge tone="muted">none</Badge>}
                    </td>
                    <td style={{ color: "var(--text-muted)" }}>
                      {n.group_id ? (groupName.get(n.group_id) ?? `#${n.group_id}`) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <p style={{ marginTop: "var(--space-4)", color: "var(--text-faint)", fontSize: "var(--fs-xs)" }}>
        Other boxes auto-register on the VLAN via the fleet beacon. Drilling into a remote
        box's nodes from here arrives with the controller proxy (next phase).
      </p>
    </>
  );
}
