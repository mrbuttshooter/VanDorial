import { useMemo, useState } from "react";
import s from "./pages.module.css";
import ui from "@/components/ui/ui.module.css";
import { Panel } from "@/components/ui/Panel";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Modal, ModalActions } from "@/components/ui/Modal";
import { Field, FieldRow, EmptyState, Spinner } from "@/components/ui/Misc";
import { IconPlus, IconRefresh, IconTrash, IconPlay, IconStop, IconLayers } from "@/components/icons";
import { useAsync } from "@/hooks/useAsync";
import { api } from "@/lib/api";
import { useToast } from "@/components/ui/Toast";
import { int } from "@/lib/format";
import type { NodeGroup, NodeGroupRequest, Transport } from "@/lib/types";

const BLANK: NodeGroupRequest = {
  name: "",
  description: "",
  dest_host: "",
  dest_port: 5060,
  transport: "udp",
  rate: 1,
  max_concurrent: 10,
  duration_mode: "fixed",
  duration_s: 180,
  duration_max_s: 0,
  match_key: "exact",
  target_calls: 0,
  target_minutes: 0,
};

/**
 * Node groups: group your nodes by customer / route. A group stores a shared
 * MADA destination + loop settings; Start fans a loop out to EVERY member node
 * (each on its own source IP + number pool, one loop per IP), and Stop stops
 * them all. Assign a node to a group from the Nodes page.
 */
export function Groups() {
  const groups = useAsync(() => api.listNodeGroups(), [], 4000);
  const toast = useToast();
  const [showNew, setShowNew] = useState(false);
  const [form, setForm] = useState<NodeGroupRequest>(BLANK);
  const [busy, setBusy] = useState<number | "new" | null>(null);

  const set = <K extends keyof NodeGroupRequest>(k: K, v: NodeGroupRequest[K]) =>
    setForm((f) => ({ ...f, [k]: v }));

  const rows = useMemo(() => groups.data?.groups ?? [], [groups.data]);

  const create = async () => {
    if (!form.name.trim()) {
      toast.error("Group name is required.");
      return;
    }
    setBusy("new");
    try {
      await api.createNodeGroup(form);
      toast.ok(`Group created · ${form.name}`);
      setShowNew(false);
      setForm(BLANK);
      groups.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(null);
    }
  };

  const start = async (g: NodeGroup) => {
    setBusy(g.id);
    try {
      const res = await api.startNodeGroup(g.id);
      const skipped = res.results.filter((r) => !r.ok);
      toast.ok(`${g.name}: started ${res.started}/${res.total} loops`);
      if (skipped.length) {
        toast.warn(
          `${skipped.length} skipped: ${skipped.map((r) => `${r.node} (${r.skipped || r.error})`).join("; ")}`,
        );
      }
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
      toast.warn(`${g.name}: stopped ${res.stopped} loops`);
      groups.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(null);
    }
  };

  const del = async (g: NodeGroup) => {
    try {
      await api.deleteNodeGroup(g.id);
      toast.warn(`Deleted ${g.name}`);
      groups.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    }
  };

  return (
    <>
      <div className={s.toolbar}>
        <span className="hud-label">{rows.length} groups</span>
        <div className={s.spacer} />
        <Button size="sm" variant="ghost" onClick={() => groups.refetch()}>
          <IconRefresh /> Refresh
        </Button>
        <Button variant="primary" onClick={() => setShowNew(true)}>
          <IconPlus /> New Group
        </Button>
      </div>

      {groups.loading && !groups.data ? (
        <Panel title="Node Groups" flush>
          <div style={{ padding: "var(--space-6)", display: "grid", placeItems: "center" }}>
            <Spinner />
          </div>
        </Panel>
      ) : rows.length === 0 ? (
        <Panel title="Node Groups" flush>
          <EmptyState
            mark="○"
            title="No groups yet"
            hint="Group nodes by customer/route with a shared MADA destination, then start a loop on every node in the group at once."
            action={
              <Button variant="primary" size="sm" onClick={() => setShowNew(true)}>
                New group
              </Button>
            }
          />
        </Panel>
      ) : (
        <div className={s.cards}>
          {rows.map((g) => {
            const running = g.running_count ?? 0;
            const members = g.nodes ?? [];
            const withPool = members.filter((m) => m.has_pool).length;
            return (
              <Panel
                key={g.id}
                title={
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
                    <IconLayers /> {g.name}
                    {running > 0 && <Badge tone="signal" pulse>{running} running</Badge>}
                  </span>
                }
                actions={
                  <div style={{ display: "flex", gap: "var(--space-2)" }}>
                    <Button size="sm" variant="primary" disabled={busy === g.id || withPool === 0}
                      onClick={() => start(g)}>
                      <IconPlay /> Start
                    </Button>
                    <Button size="sm" variant="ghost" disabled={busy === g.id || running === 0}
                      onClick={() => stop(g)}>
                      <IconStop /> Stop
                    </Button>
                    <Button size="sm" variant="ghost" icon title="Delete group" onClick={() => del(g)}>
                      <IconTrash />
                    </Button>
                  </div>
                }
              >
                <div style={{ fontSize: "var(--fs-xs)", color: "var(--text-muted)", marginBottom: "var(--space-3)" }}>
                  Route → <strong style={{ color: "var(--text)" }}>{g.dest_host || "— no destination —"}:{g.dest_port}</strong>
                  {" · "}{g.rate} cps · {g.duration_s}s holds
                  {g.target_calls ? ` · ${int(g.target_calls)} calls/node` : ""}
                </div>
                {members.length === 0 ? (
                  <EmptyState mark="○" title="No nodes in this group" hint="Assign nodes to this group from the Nodes page." />
                ) : (
                  <div className={ui.tableWrap}>
                    <table className={ui.table}>
                      <thead>
                        <tr><th>Node</th><th>Source IP</th><th>Zones</th><th className={ui.numCell}>Pool</th></tr>
                      </thead>
                      <tbody>
                        {members.map((m) => (
                          <tr key={m.id}>
                            <td style={{ color: "var(--text-bright)" }}>{m.name}</td>
                            <td style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono, monospace)" }}>{m.ip}</td>
                            <td style={{ color: "var(--text-muted)" }}>
                              {m.has_pool ? `${m.origin_zone} → ${m.dest_zone}` : "— no pool —"}
                            </td>
                            <td className={ui.numCell}>
                              {m.has_pool ? <Badge tone="signal">{int(m.pool_count)}</Badge> : <Badge tone="muted">none</Badge>}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Panel>
            );
          })}
        </div>
      )}

      <Modal
        open={showNew}
        title={<><IconLayers /> New Node Group</>}
        onClose={() => { setShowNew(false); setForm(BLANK); }}
        footer={<ModalActions onCancel={() => { setShowNew(false); setForm(BLANK); }} onConfirm={create} confirmLabel="Create" disabled={busy === "new"} />}
      >
        <FieldRow>
          <Field label="Group name" hint="e.g. a customer or route">
            <input value={form.name} onChange={(e) => set("name", e.target.value)} placeholder="guinea-route" />
          </Field>
          <Field label="Description">
            <input value={form.description} onChange={(e) => set("description", e.target.value)} />
          </Field>
        </FieldRow>
        <div className={s.formSection}>Shared destination (MADA switch)</div>
        <FieldRow>
          <Field label="Destination host">
            <input value={form.dest_host} onChange={(e) => set("dest_host", e.target.value)} placeholder="10.20.8.40" />
          </Field>
          <Field label="Destination port">
            <input type="number" value={form.dest_port} onChange={(e) => set("dest_port", Number(e.target.value))} />
          </Field>
          <Field label="Transport">
            <select value={form.transport} onChange={(e) => set("transport", e.target.value as Transport)}>
              <option value="udp">UDP</option>
              <option value="tcp">TCP</option>
              <option value="tls">TLS</option>
            </select>
          </Field>
        </FieldRow>
        <div className={s.formSection}>Loop settings (per node)</div>
        <FieldRow>
          <Field label="Call rate (cps)">
            <input type="number" step="0.1" value={form.rate} onChange={(e) => set("rate", Number(e.target.value))} />
          </Field>
          <Field label="Max concurrent">
            <input type="number" value={form.max_concurrent} onChange={(e) => set("max_concurrent", Number(e.target.value))} />
          </Field>
          <Field label="Duration (s)">
            <input type="number" value={form.duration_s} onChange={(e) => set("duration_s", Number(e.target.value))} />
          </Field>
        </FieldRow>
        <FieldRow>
          <Field label="Target calls / node" hint="0 = until stopped">
            <input type="number" value={form.target_calls} onChange={(e) => set("target_calls", Number(e.target.value))} />
          </Field>
          <Field label="Match key" hint="exact or suffixN">
            <input value={form.match_key} onChange={(e) => set("match_key", e.target.value)} placeholder="exact" />
          </Field>
        </FieldRow>
      </Modal>
    </>
  );
}
