import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Panel } from "@/components/ui/Panel";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Modal, ModalActions } from "@/components/ui/Modal";
import { EmptyState, Spinner } from "@/components/ui/Misc";
import { IconPlay, IconStop, IconLoop } from "@/components/icons";
import { useAsync } from "@/hooks/useAsync";
import { api } from "@/lib/api";
import { useToast } from "@/components/ui/Toast";
import { int } from "@/lib/format";
import type { NodeGroup } from "@/lib/types";

export function Routes() {
  const groups = useAsync(() => api.listNodeGroups(), [], 4000);
  const toast = useToast();
  const [busy, setBusy] = useState<number | null>(null);
  const [pick, setPick] = useState<NodeGroup | null>(null);
  const [sel, setSel] = useState<Set<number>>(new Set());

  const rows = useMemo(() => groups.data?.groups ?? [], [groups.data]);

  const openStart = (g: NodeGroup) => {
    setPick(g);
    // default: select every member that has a number pool
    setSel(new Set((g.nodes ?? []).filter((n) => n.has_pool).map((n) => n.id)));
  };

  const confirmStart = async () => {
    if (!pick) return;
    const ids = [...sel];
    if (ids.length === 0) {
      toast.error("Pick at least one node to run.");
      return;
    }
    setBusy(pick.id);
    try {
      const all = (pick.nodes ?? []).filter((n) => n.has_pool).length;
      const res = await api.startNodeGroup(pick.id, ids.length === all ? undefined : ids);
      toast.ok(`${pick.name}: started ${res.started}/${res.total}`);
      const skipped = res.results.filter((r) => !r.ok);
      if (skipped.length) toast.warn(`${skipped.length} skipped: ${skipped.map((r) => r.node).join(", ")}`);
      setPick(null);
      groups.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(null);
    }
  };

  const stop = async (g: NodeGroup) => {
    setBusy(g.id);
    try {
      const res = await api.stopNodeGroup(g.id);
      toast.warn(`${g.name}: stopped ${res.stopped}`);
      groups.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(null);
    }
  };

  const toggle = (id: number) =>
    setSel((s) => {
      const n = new Set(s);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });

  return (
    <>
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", marginBottom: "var(--space-4)" }}>
        <div>
          <h1 style={{ margin: 0, color: "var(--text-bright)" }}>Routes</h1>
          <p style={{ margin: "4px 0 0", color: "var(--text-muted)", fontSize: "var(--fs-sm)" }}>
            A customer/route = a MADA destination + its nodes. Start all, or pick a subset.
          </p>
        </div>
        <Link to="/groups"><Button variant="primary" size="sm">Manage routes</Button></Link>
      </div>

      {groups.loading && !groups.data ? (
        <Panel title="Routes" flush><div style={{ padding: "var(--space-6)", display: "grid", placeItems: "center" }}><Spinner /></div></Panel>
      ) : rows.length === 0 ? (
        <Panel title="Routes" flush>
          <EmptyState mark="○" title="No routes yet" hint="Create a route (a customer + MADA destination) and assign nodes."
            action={<Link to="/groups"><Button variant="primary" size="sm">New route</Button></Link>} />
        </Panel>
      ) : (
        rows.map((g) => {
          const running = g.running_count ?? 0;
          const withPool = (g.nodes ?? []).filter((n) => n.has_pool).length;
          return (
            <div key={g.id} style={{ marginBottom: "var(--space-3)" }}>
              <Panel
                title={<span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}><IconLoop width={16} height={16} /> {g.name}
                  {running > 0 && <Badge tone="signal" pulse>{running} running</Badge>}</span>}
                actions={
                  <div style={{ display: "flex", gap: "var(--space-2)" }}>
                    <Button size="sm" variant="primary" disabled={busy === g.id || withPool === 0} onClick={() => openStart(g)}><IconPlay /> Start…</Button>
                    <Button size="sm" variant="ghost" disabled={busy === g.id || running === 0} onClick={() => stop(g)}><IconStop /> Stop</Button>
                  </div>
                }
              >
                <div style={{ fontSize: "var(--fs-xs)", color: "var(--text-muted)", marginBottom: "var(--space-2)", fontFamily: "var(--font-mono, monospace)" }}>
                  → {g.dest_host || "— no destination —"}:{g.dest_port} · {g.rate} cps · {g.duration_s}s · {int(g.target_calls)} calls/node
                </div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--space-2)" }}>
                  {(g.nodes ?? []).map((n) => (
                    <Badge key={n.id} tone={n.has_pool ? "cyan" : "muted"}>
                      {n.name} · {n.ip}
                    </Badge>
                  ))}
                  {(g.nodes ?? []).length === 0 && <span style={{ color: "var(--text-faint)", fontSize: "var(--fs-xs)" }}>no nodes — assign some on Manage routes</span>}
                </div>
              </Panel>
            </div>
          );
        })
      )}

      <Modal
        open={!!pick}
        title={<>Start {pick?.name} — pick nodes</>}
        onClose={() => setPick(null)}
        footer={<ModalActions onCancel={() => setPick(null)} onConfirm={confirmStart} confirmLabel={`Start ${sel.size}`} disabled={busy === pick?.id} />}
      >
        <p style={{ color: "var(--text-muted)", fontSize: "var(--fs-sm)", marginTop: 0 }}>
          Each selected node runs one loop on its own IP. Nodes without a pool can't run.
        </p>
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {(pick?.nodes ?? []).map((n) => (
            <label key={n.id} style={{ display: "flex", alignItems: "center", gap: 10, opacity: n.has_pool ? 1 : 0.45 }}>
              <input type="checkbox" disabled={!n.has_pool} checked={sel.has(n.id)} onChange={() => toggle(n.id)} />
              <span style={{ color: "var(--text-bright)" }}>{n.name}</span>
              <span style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono, monospace)", fontSize: "var(--fs-xs)" }}>{n.ip}</span>
              <span style={{ marginLeft: "auto", color: "var(--text-faint)", fontSize: "var(--fs-xs)" }}>
                {n.has_pool ? `${n.origin_zone} → ${n.dest_zone}` : "no pool"}
              </span>
            </label>
          ))}
        </div>
      </Modal>
    </>
  );
}
