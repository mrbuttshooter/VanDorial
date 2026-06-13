import { Fragment, useMemo, useState } from "react";
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
 * Node groups: a named set of nodes (group by customer / route). The loop RECIPE
 * lives in a preset — to start a group you run a preset on it from the Loops page
 * (Run → A group). Each row expands to show the group's member IPs; Stop all
 * stops every running loop on those IPs. Assign nodes from the Nodes page.
 */
export function Groups() {
  const groups = useAsync(() => api.listNodeGroups(), [], 4000);
  const toast = useToast();
  const [showNew, setShowNew] = useState(false);
  const [form, setForm] = useState<NodeGroupRequest>(BLANK);
  const [busy, setBusy] = useState<number | "new" | null>(null);
  const [open, setOpen] = useState<Set<number>>(new Set());

  const set = <K extends keyof NodeGroupRequest>(k: K, v: NodeGroupRequest[K]) =>
    setForm((f) => ({ ...f, [k]: v }));

  const rows = useMemo(() => groups.data?.groups ?? [], [groups.data]);

  const toggle = (id: number) =>
    setOpen((o) => {
      const n = new Set(o);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

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

      <Panel title="Node Groups" flush>
        {groups.loading && !groups.data ? (
          <div style={{ padding: "var(--space-6)", display: "grid", placeItems: "center" }}>
            <Spinner />
          </div>
        ) : rows.length === 0 ? (
          <EmptyState
            mark="○"
            title="No groups yet"
            hint="A group is just a set of nodes (by customer/route). Create one, assign nodes on the Nodes page, then run a preset on the group from the Loops page."
            action={
              <Button variant="primary" size="sm" onClick={() => setShowNew(true)}>
                New group
              </Button>
            }
          />
        ) : (
          <div className={ui.tableWrap}>
            <table className={ui.table}>
              <thead>
                <tr>
                  <th style={{ width: 28 }}></th>
                  <th>Group</th>
                  <th className={ui.numCell}>Nodes</th>
                  <th className={ui.numCell}>Pooled</th>
                  <th>Running</th>
                  <th>Description</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((g) => {
                  const members = g.nodes ?? [];
                  const withPool = members.filter((m) => m.has_pool).length;
                  const running = g.running_count ?? 0;
                  const isOpen = open.has(g.id);
                  return (
                    <Fragment key={g.id}>
                      <tr onClick={() => toggle(g.id)} style={{ cursor: "pointer" }}>
                        <td style={{ color: "var(--text-faint)", textAlign: "center" }}>
                          {isOpen ? "▾" : "▸"}
                        </td>
                        <td style={{ color: "var(--text-bright)", fontWeight: 600 }}>
                          <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
                            <IconLayers /> {g.name}
                          </span>
                        </td>
                        <td className={ui.numCell}>{int(members.length)}</td>
                        <td className={ui.numCell}>{int(withPool)}</td>
                        <td>
                          {running > 0 ? (
                            <Badge tone="signal" pulse>{running} running</Badge>
                          ) : (
                            <span style={{ color: "var(--text-faint)" }}>idle</span>
                          )}
                        </td>
                        <td style={{ color: "var(--text-muted)" }}>{g.description || "—"}</td>
                        <td style={{ textAlign: "right", whiteSpace: "nowrap" }} onClick={(e) => e.stopPropagation()}>
                          <Button size="sm" variant="ghost" disabled={busy === g.id || running === 0} onClick={() => stop(g)}>
                            <IconStop /> Stop all
                          </Button>
                          <Button size="sm" variant="ghost" icon title="Delete group" onClick={() => del(g)}>
                            <IconTrash />
                          </Button>
                        </td>
                      </tr>
                      {isOpen && (
                        <tr>
                          <td colSpan={7} style={{ background: "var(--bg-inset)", padding: "var(--space-3) var(--space-4)" }}>
                            {members.length === 0 ? (
                              <div style={{ fontSize: "var(--fs-xs)", color: "var(--text-muted)" }}>
                                No nodes in this group — assign some on the <Link to="/nodes">Nodes page</Link>.
                              </div>
                            ) : (
                              <table className={ui.table} style={{ margin: 0 }}>
                                <thead>
                                  <tr>
                                    <th>Node</th>
                                    <th>Source IP</th>
                                    <th>Origin → Drop zone</th>
                                    <th className={ui.numCell}>Pool</th>
                                  </tr>
                                </thead>
                                <tbody>
                                  {members.map((m) => (
                                    <tr key={m.id}>
                                      <td style={{ color: "var(--text-bright)" }}>{m.name}</td>
                                      <td style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono, monospace)" }}>{m.ip}</td>
                                      <td style={{ color: "var(--text-muted)" }}>
                                        {m.has_pool ? (
                                          <>
                                            {m.origin_zone}
                                            {m.origin_code ? ` (${m.origin_code})` : ""} → {m.dest_zone}
                                            {m.dest_code ? ` (${m.dest_code})` : ""}
                                          </>
                                        ) : "— no pool —"}
                                      </td>
                                      <td className={ui.numCell}>
                                        {m.has_pool ? <Badge tone="signal">{int(m.pool_count)}</Badge> : <Badge tone="muted">none</Badge>}
                                      </td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            )}
                            <div style={{ fontSize: "var(--fs-xs)", color: "var(--text-faint)", marginTop: "var(--space-2)" }}>
                              To start: <Link to="/loops">run a preset on this group</Link> (Loops → Run → A group).
                            </div>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

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
          by running a preset (Loops → Run → A group). The destination, ACD and rate come
          from the preset.
        </p>
      </Modal>
    </>
  );
}
