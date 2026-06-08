import { useState } from "react";
import { useNavigate } from "react-router-dom";
import s from "./pages.module.css";
import ui from "@/components/ui/ui.module.css";
import { Panel } from "@/components/ui/Panel";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Modal, ModalActions } from "@/components/ui/Modal";
import { Field, FieldRow, EmptyState, Spinner } from "@/components/ui/Misc";
import { IconPlus, IconRefresh, IconTrash, IconSliders, IconBolt } from "@/components/icons";
import { useAsync } from "@/hooks/useAsync";
import { useToast } from "@/components/ui/Toast";
import { ago } from "@/lib/format";
import { fleetApi } from "@/fleet/fleetApi";
import { useFleetScope } from "@/fleet/scope";
import type { CreateNodeRequest, GroupView, NodeView, UpdateNodeRequest } from "@/fleet/types";

/* Node inventory: the controller's worker registry (design §4 Nodes). Supports
   add / edit / delete, an on-demand health probe (POST /api/nodes/{id}/check),
   and "drill in" which sets the global node scope so the existing console pages
   operate against that node via the controller proxy. */

interface NodeForm {
  name: string;
  address: string;
  group_id: number | null;
  api_key: string;
  enabled: boolean;
}

const BLANK: NodeForm = {
  name: "",
  address: "",
  group_id: null,
  api_key: "",
  enabled: true,
};

export function Nodes() {
  const navigate = useNavigate();
  const { selectNode } = useFleetScope();
  const toast = useToast();

  const nodes = useAsync(() => fleetApi.listNodes(), [], 4000);
  const groups = useAsync(() => fleetApi.listGroups(), []);

  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<NodeView | null>(null);
  const [form, setForm] = useState<NodeForm>(BLANK);
  const [confirmDelete, setConfirmDelete] = useState<NodeView | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  const set = <K extends keyof NodeForm>(k: K, v: NodeForm[K]) =>
    setForm((f) => ({ ...f, [k]: v }));

  const groupList = groups.data?.groups ?? [];
  const rows = nodes.data?.nodes ?? [];

  const openNew = () => {
    setEditing(null);
    setForm(BLANK);
    setShowForm(true);
  };

  const openEdit = (n: NodeView) => {
    setEditing(n);
    setForm({
      name: n.name,
      address: n.address,
      group_id: n.group_id,
      api_key: "", // never echo the stored key; blank = keep existing
      enabled: n.enabled,
    });
    setShowForm(true);
  };

  const save = async () => {
    if (!form.name.trim()) {
      toast.error("Node name is required.");
      return;
    }
    if (!form.address.trim()) {
      toast.error("Node address is required.");
      return;
    }
    try {
      if (editing) {
        const patch: UpdateNodeRequest = {
          name: form.name,
          address: form.address,
          group_id: form.group_id,
          enabled: form.enabled,
        };
        if (form.api_key.trim()) patch.api_key = form.api_key.trim();
        await fleetApi.updateNode(editing.id, patch);
        toast.ok(`Node updated · ${form.name}`);
      } else {
        if (!form.api_key.trim()) {
          toast.error("API key is required for a new node.");
          return;
        }
        const req: CreateNodeRequest = {
          name: form.name,
          address: form.address,
          group_id: form.group_id,
          api_key: form.api_key.trim(),
          enabled: form.enabled,
        };
        await fleetApi.createNode(req);
        toast.ok(`Node added · ${form.name}`);
      }
      setShowForm(false);
      setEditing(null);
      setForm(BLANK);
      nodes.refetch();
    } catch (e) {
      toast.error(`Save failed: ${e instanceof Error ? e.message : e}`);
    }
  };

  const remove = async () => {
    if (!confirmDelete) return;
    try {
      await fleetApi.deleteNode(confirmDelete.id);
      toast.warn(`Removed ${confirmDelete.name}`);
      setConfirmDelete(null);
      nodes.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    }
  };

  const check = async (n: NodeView) => {
    setBusyId(n.id);
    try {
      const updated = await fleetApi.checkNode(n.id);
      toast[updated.online ? "ok" : "warn"](
        updated.online ? `${n.name} online · v${updated.version ?? "?"}` : `${n.name} unreachable`,
      );
      nodes.refetch();
    } catch (e) {
      toast.error(`Probe failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusyId(null);
    }
  };

  const drillIn = (n: NodeView) => {
    selectNode(n.id);
    toast.info(`Scope → ${n.name}. Console pages now target this node.`);
    navigate("/");
  };

  return (
    <>
      <div className={s.toolbar}>
        <span className="hud-label">
          {rows.length} node{rows.length === 1 ? "" : "s"} ·{" "}
          {rows.filter((n) => n.online).length} online
        </span>
        <div className={s.spacer} />
        <Button size="sm" variant="ghost" onClick={() => nodes.refetch()}>
          <IconRefresh /> Refresh
        </Button>
        <Button variant="primary" onClick={openNew}>
          <IconPlus /> Add Node
        </Button>
      </div>

      <Panel title="Node Inventory" flush live>
        {nodes.loading && !nodes.data ? (
          <div style={{ padding: "var(--space-6)", display: "grid", placeItems: "center" }}>
            <Spinner />
          </div>
        ) : rows.length === 0 ? (
          <EmptyState
            mark="○"
            title="No nodes registered"
            hint="Register a GenCall worker so the controller can drive and aggregate it."
            action={
              <Button variant="primary" size="sm" onClick={openNew}>
                Add node
              </Button>
            }
          />
        ) : (
          <div className={ui.tableWrap}>
            <table className={ui.table}>
              <thead>
                <tr>
                  <th>Node</th>
                  <th>Address</th>
                  <th>Group</th>
                  <th>Version</th>
                  <th className={ui.numCell}>Tests</th>
                  <th>Last seen</th>
                  <th>Status</th>
                  <th style={{ textAlign: "right" }}>Control</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((n) => {
                  const tone = !n.enabled ? "muted" : n.online ? "signal" : "crit";
                  const label = !n.enabled ? "disabled" : n.online ? "online" : "offline";
                  return (
                    <tr key={n.id}>
                      <td style={{ color: "var(--text-bright)", fontWeight: 600 }}>{n.name}</td>
                      <td style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                        {n.address}
                      </td>
                      <td style={{ color: "var(--text-muted)" }}>{n.group_name ?? "—"}</td>
                      <td style={{ color: "var(--text-muted)" }}>{n.version ?? "—"}</td>
                      <td className={ui.numCell}>{n.active_tests}</td>
                      <td style={{ color: "var(--text-muted)" }} title={n.error || undefined}>
                        {ago(n.last_seen)}
                      </td>
                      <td>
                        <Badge tone={tone} pulse={n.online && n.active_tests > 0}>
                          {label}
                        </Badge>
                      </td>
                      <td>
                        <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
                          <Button
                            size="sm"
                            variant="ghost"
                            icon
                            title="Drill in (set node scope)"
                            onClick={() => drillIn(n)}
                          >
                            <IconBolt />
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            icon
                            title="Health probe"
                            disabled={busyId === n.id}
                            onClick={() => check(n)}
                          >
                            <IconRefresh />
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            icon
                            title="Edit"
                            onClick={() => openEdit(n)}
                          >
                            <IconSliders />
                          </Button>
                          <Button
                            size="sm"
                            variant="danger"
                            icon
                            title="Remove"
                            onClick={() => setConfirmDelete(n)}
                          >
                            <IconTrash />
                          </Button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      {/* ---- Add / edit node modal ---- */}
      <Modal
        open={showForm}
        title={editing ? <>Edit Node · {editing.name}</> : <><IconPlus /> Add Node</>}
        onClose={() => setShowForm(false)}
        footer={
          <ModalActions
            onCancel={() => setShowForm(false)}
            onConfirm={save}
            confirmLabel={editing ? "Save" : "Add node"}
          />
        }
      >
        <FieldRow>
          <Field label="Name">
            <input value={form.name} onChange={(e) => set("name", e.target.value)} placeholder="ny-gen-05" />
          </Field>
          <Field label="Group">
            <GroupSelect
              groups={groupList}
              value={form.group_id}
              onChange={(v) => set("group_id", v)}
            />
          </Field>
        </FieldRow>
        <Field label="Address" hint="Worker base URL, e.g. https://10.20.1.15:8080">
          <input
            value={form.address}
            onChange={(e) => set("address", e.target.value)}
            placeholder="https://10.20.1.15:8080"
          />
        </Field>
        <Field
          label="API key"
          hint={editing ? "Leave blank to keep the current key." : "The worker's X-API-Key."}
        >
          <input
            type="password"
            value={form.api_key}
            onChange={(e) => set("api_key", e.target.value)}
            placeholder={editing ? "•••••••• (unchanged)" : "nodekey-…"}
            autoComplete="off"
          />
        </Field>
        <Field label="Enabled" hint="Disabled nodes are not polled or dispatched to.">
          <label style={{ display: "inline-flex", alignItems: "center", gap: 8, color: "var(--text)" }}>
            <input
              type="checkbox"
              checked={form.enabled}
              onChange={(e) => set("enabled", e.target.checked)}
              style={{ accentColor: "var(--signal)" }}
            />
            Poll & dispatch to this node
          </label>
        </Field>
      </Modal>

      {/* ---- Delete confirm ---- */}
      <Modal
        open={confirmDelete !== null}
        title="Remove Node"
        onClose={() => setConfirmDelete(null)}
        footer={
          <ModalActions
            onCancel={() => setConfirmDelete(null)}
            onConfirm={remove}
            confirmLabel="Remove"
            danger
          />
        }
      >
        <p style={{ color: "var(--text)" }}>
          Remove <strong style={{ color: "var(--text-bright)" }}>{confirmDelete?.name}</strong> from
          the inventory? Running tests on the node are not stopped — only the controller's record is
          deleted.
        </p>
      </Modal>
    </>
  );
}

function GroupSelect({
  groups,
  value,
  onChange,
}: {
  groups: GroupView[];
  value: number | null;
  onChange: (v: number | null) => void;
}) {
  return (
    <select
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
    >
      <option value="">— ungrouped —</option>
      {groups.map((g) => (
        <option key={g.id} value={g.id}>
          {g.name}
        </option>
      ))}
    </select>
  );
}
