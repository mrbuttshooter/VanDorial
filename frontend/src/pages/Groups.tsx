import { useMemo, useState } from "react";
import s from "./pages.module.css";
import ui from "@/components/ui/ui.module.css";
import { Panel } from "@/components/ui/Panel";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Modal, ModalActions } from "@/components/ui/Modal";
import { Field, FieldRow, EmptyState, Spinner } from "@/components/ui/Misc";
import { IconPlus, IconRefresh, IconTrash, IconStop, IconLayers } from "@/components/icons";
import { useAsync } from "@/hooks/useAsync";
import { api } from "@/lib/api";
import { useToast } from "@/components/ui/Toast";
import { int } from "@/lib/format";
import { Link } from "react-router-dom";
import type { NodeGroup, NodeGroupRequest } from "@/lib/types";

const BLANK: NodeGroupRequest = { name: "", description: "" };

/**
 * Node groups: just a named set of nodes (group by customer / route). The loop
 * RECIPE lives in a preset — to start a group you run a preset on it from the
 * Loops page (Run → A group). Stop here stops every running loop on the group's
 * member IPs at once. Assign a node to a group from the Nodes page.
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
    if (!form.name?.trim()) {
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
            hint="A group is just a set of nodes (by customer/route). Create one, assign nodes to it on the Nodes page, then run a preset on the whole group from the Loops page."
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
                    <Button size="sm" variant="ghost" disabled={busy === g.id || running === 0}
                      onClick={() => stop(g)}>
                      <IconStop /> Stop all
                    </Button>
                    <Button size="sm" variant="ghost" icon title="Delete group" onClick={() => del(g)}>
                      <IconTrash />
                    </Button>
                  </div>
                }
              >
                <div style={{ fontSize: "var(--fs-xs)", color: "var(--text-muted)", marginBottom: "var(--space-3)" }}>
                  {members.length} node{members.length === 1 ? "" : "s"} · {withPool} with a pool
                  {g.description ? ` · ${g.description}` : ""}
                  {" — "}
                  <Link to="/loops">run a preset on this group</Link> to start it.
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
          <Field label="Description" hint="Optional.">
            <input value={form.description ?? ""} onChange={(e) => set("description", e.target.value)} />
          </Field>
        </FieldRow>
        <p className={s.advancedSummary}>
          A group is just a set of nodes. Add nodes to it on the Nodes page, then start it
          by running a preset (Loops → Run → A group). The loop's destination, ACD and rate
          come from the preset.
        </p>
      </Modal>
    </>
  );
}
