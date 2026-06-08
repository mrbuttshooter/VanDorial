import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import s from "./pages.module.css";
import f from "./fleet.module.css";
import { Panel } from "@/components/ui/Panel";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Modal, ModalActions } from "@/components/ui/Modal";
import { Field, FieldRow, EmptyState, Spinner } from "@/components/ui/Misc";
import { IconPlus, IconRefresh, IconTrash, IconSliders, IconPlay, IconLayers } from "@/components/icons";
import { useAsync } from "@/hooks/useAsync";
import { useToast } from "@/components/ui/Toast";
import { fleetApi } from "@/fleet/fleetApi";
import { useFleetScope } from "@/fleet/scope";
import type {
  CreateGroupRequest,
  FleetLaunchRequest,
  GroupView,
  NodeView,
  RateMode,
  UpdateGroupRequest,
} from "@/fleet/types";
import type { Transport } from "@/lib/types";

/* Group management (design §4 Groups + §7): CRUD, membership editing, and the
   "Launch campaign on group" modal that POSTs /api/fleet/launch with a rate
   mode of per_node | total (design §5 rate model). */

export function Groups() {
  const navigate = useNavigate();
  const { scope, selectGroup } = useFleetScope();
  const toast = useToast();

  const groups = useAsync(() => fleetApi.listGroups(), [], 4000);
  const nodes = useAsync(() => fleetApi.listNodes(), [], 4000);

  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<GroupView | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [confirmDelete, setConfirmDelete] = useState<GroupView | null>(null);

  // Membership editor (open from a group card).
  const [members, setMembers] = useState<GroupView | null>(null);
  const [memberIds, setMemberIds] = useState<Set<number>>(new Set());

  // Launch-on-group campaign.
  const [launchFor, setLaunchFor] = useState<GroupView | null>(null);

  const groupList = groups.data?.groups ?? [];
  const nodeList = nodes.data?.nodes ?? [];

  const openNew = () => {
    setEditing(null);
    setName("");
    setDescription("");
    setShowForm(true);
  };
  const openEdit = (g: GroupView) => {
    setEditing(g);
    setName(g.name);
    setDescription(g.description);
    setShowForm(true);
  };

  const save = async () => {
    if (!name.trim()) {
      toast.error("Group name is required.");
      return;
    }
    try {
      if (editing) {
        const patch: UpdateGroupRequest = { name, description };
        await fleetApi.updateGroup(editing.id, patch);
        toast.ok(`Group updated · ${name}`);
      } else {
        const req: CreateGroupRequest = { name, description };
        await fleetApi.createGroup(req);
        toast.ok(`Group created · ${name}`);
      }
      setShowForm(false);
      groups.refetch();
    } catch (e) {
      toast.error(`Save failed: ${e instanceof Error ? e.message : e}`);
    }
  };

  const remove = async () => {
    if (!confirmDelete) return;
    try {
      await fleetApi.deleteGroup(confirmDelete.id);
      toast.warn(`Deleted ${confirmDelete.name}`);
      setConfirmDelete(null);
      groups.refetch();
      nodes.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    }
  };

  const openMembers = (g: GroupView) => {
    setMembers(g);
    setMemberIds(new Set(g.node_ids));
  };
  const toggleMember = (id: number) =>
    setMemberIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const saveMembers = async () => {
    if (!members) return;
    try {
      await fleetApi.updateGroup(members.id, { node_ids: [...memberIds] });
      toast.ok(`Membership updated · ${members.name}`);
      setMembers(null);
      groups.refetch();
      nodes.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    }
  };

  return (
    <>
      <div className={s.toolbar}>
        <span className="hud-label">
          {groupList.length} group{groupList.length === 1 ? "" : "s"}
        </span>
        <div className={s.spacer} />
        <Button size="sm" variant="ghost" onClick={() => groups.refetch()}>
          <IconRefresh /> Refresh
        </Button>
        <Button variant="primary" onClick={openNew}>
          <IconPlus /> New Group
        </Button>
      </div>

      <Panel title="Groups" flush>
        {groups.loading && !groups.data ? (
          <div style={{ padding: "var(--space-6)", display: "grid", placeItems: "center" }}>
            <Spinner />
          </div>
        ) : groupList.length === 0 ? (
          <EmptyState
            mark="//"
            title="No groups yet"
            hint="Group nodes by region or role, then launch a campaign across the whole group."
            action={
              <Button variant="primary" size="sm" onClick={openNew}>
                New group
              </Button>
            }
          />
        ) : (
          <div className={s.cards} style={{ padding: "var(--space-4)" }}>
            {groupList.map((g) => (
              <div
                key={g.id}
                className={s.card}
                style={
                  scope.kind === "group" && scope.groupId === g.id
                    ? { borderColor: "var(--signal-dim)" }
                    : undefined
                }
              >
                <div className={s.cardTop}>
                  <span className={s.cardName}>{g.name}</span>
                  <Badge tone={g.online_count === g.total_count ? "signal" : "amber"}>
                    {g.online_count}/{g.total_count} up
                  </Badge>
                </div>
                <div className={s.cardDesc}>{g.description || "No description."}</div>
                <div className={s.cardActions}>
                  <Button
                    size="sm"
                    variant="primary"
                    onClick={() => setLaunchFor(g)}
                    disabled={g.online_count === 0}
                    title={g.online_count === 0 ? "No online nodes" : "Launch campaign on group"}
                  >
                    <IconPlay /> Launch
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => openMembers(g)}>
                    <IconLayers /> Members
                  </Button>
                  <Button size="sm" variant="ghost" icon title="Edit" onClick={() => openEdit(g)}>
                    <IconSliders />
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => {
                      selectGroup(g.id);
                      navigate("/fleet");
                    }}
                  >
                    Scope
                  </Button>
                  <div className={s.spacer} />
                  <Button
                    size="sm"
                    variant="danger"
                    icon
                    title="Delete"
                    onClick={() => setConfirmDelete(g)}
                  >
                    <IconTrash />
                  </Button>
                </div>
              </div>
            ))}
          </div>
        )}
      </Panel>

      {/* ---- Create / edit group ---- */}
      <Modal
        open={showForm}
        title={editing ? <>Edit Group · {editing.name}</> : <><IconPlus /> New Group</>}
        onClose={() => setShowForm(false)}
        footer={
          <ModalActions
            onCancel={() => setShowForm(false)}
            onConfirm={save}
            confirmLabel={editing ? "Save" : "Create"}
          />
        }
      >
        <Field label="Name">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="NY-edge" />
        </Field>
        <Field label="Description" hint="Optional — what this group is for.">
          <input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="New York edge generators"
          />
        </Field>
      </Modal>

      {/* ---- Delete confirm ---- */}
      <Modal
        open={confirmDelete !== null}
        title="Delete Group"
        onClose={() => setConfirmDelete(null)}
        footer={
          <ModalActions
            onCancel={() => setConfirmDelete(null)}
            onConfirm={remove}
            confirmLabel="Delete"
            danger
          />
        }
      >
        <p style={{ color: "var(--text)" }}>
          Delete <strong style={{ color: "var(--text-bright)" }}>{confirmDelete?.name}</strong>? Its{" "}
          {confirmDelete?.total_count ?? 0} node(s) are not deleted — they become ungrouped.
        </p>
      </Modal>

      {/* ---- Membership editor ---- */}
      <Modal
        open={members !== null}
        title={<><IconLayers /> Members · {members?.name}</>}
        onClose={() => setMembers(null)}
        footer={
          <ModalActions onCancel={() => setMembers(null)} onConfirm={saveMembers} confirmLabel="Save" />
        }
      >
        <div className={f.memberCols}>
          <div>
            <div className="hud-label" style={{ marginBottom: 8 }}>
              {memberIds.size} of {nodeList.length} node(s) selected
            </div>
            <div className={f.memberList}>
              {nodeList.length === 0 ? (
                <span style={{ color: "var(--text-faint)", fontSize: "var(--fs-sm)" }}>
                  No nodes registered.
                </span>
              ) : (
                nodeList.map((n) => (
                  <label key={n.id} className={f.memberRow}>
                    <input
                      type="checkbox"
                      checked={memberIds.has(n.id)}
                      onChange={() => toggleMember(n.id)}
                    />
                    {n.name}
                    <span className={f.memberMeta}>
                      {n.group_name && n.group_id !== members?.id ? `in ${n.group_name}` : n.address}
                    </span>
                  </label>
                ))
              )}
            </div>
          </div>
        </div>
      </Modal>

      {/* ---- Launch campaign on group ---- */}
      <LaunchModal
        group={launchFor}
        nodes={nodeList}
        onClose={() => setLaunchFor(null)}
        onLaunched={() => {
          setLaunchFor(null);
          navigate("/fleet");
        }}
      />
    </>
  );
}

/* ---- Launch-on-group campaign modal ------------------------------------- */
interface LaunchForm {
  name: string;
  scenario: string;
  remote_host: string;
  remote_port: number;
  transport: Transport;
  rate_mode: RateMode;
  rate_value: number;
  call_limit: number;
  max_calls: number;
  duration: number;
}

const LAUNCH_BLANK: LaunchForm = {
  name: "",
  scenario: "basic_call",
  remote_host: "",
  remote_port: 5060,
  transport: "udp",
  rate_mode: "per_node",
  rate_value: 20,
  call_limit: 20,
  max_calls: 0,
  duration: 0,
};

function LaunchModal({
  group,
  nodes,
  onClose,
  onLaunched,
}: {
  group: GroupView | null;
  nodes: NodeView[];
  onClose: () => void;
  onLaunched: () => void;
}) {
  const toast = useToast();
  const [form, setForm] = useState<LaunchForm>(LAUNCH_BLANK);
  const [submitting, setSubmitting] = useState(false);

  // Reset the form each time the modal opens for a (possibly different) group.
  useEffect(() => {
    if (group) {
      setForm({ ...LAUNCH_BLANK, name: `${group.name}-campaign` });
    }
  }, [group]);

  const set = <K extends keyof LaunchForm>(k: K, v: LaunchForm[K]) =>
    setForm((s) => ({ ...s, [k]: v }));

  const onlineCount = group
    ? nodes.filter((n) => group.node_ids.includes(n.id) && n.online && n.enabled).length
    : 0;

  // Effective total cps preview per the §5 rate model.
  const totalCps =
    form.rate_mode === "per_node" ? form.rate_value * onlineCount : form.rate_value;
  // For "total" mode the controller splits to hundredths (remainder to the
  // first nodes); show ~2dp so the preview matches what nodes actually get.
  const perNodeCps =
    onlineCount === 0
      ? 0
      : form.rate_mode === "per_node"
        ? form.rate_value
        : Math.round((form.rate_value / onlineCount) * 100) / 100;

  const launch = async () => {
    if (!group) return;
    if (!form.remote_host.trim()) {
      toast.error("Destination host is required.");
      return;
    }
    if (form.rate_value <= 0) {
      toast.error("Rate must be greater than zero.");
      return;
    }
    setSubmitting(true);
    try {
      const req: FleetLaunchRequest = {
        name: form.name || undefined,
        group_id: group.id,
        scenario: form.scenario,
        destination: {
          remote_host: form.remote_host.trim(),
          remote_port: form.remote_port,
          transport: form.transport,
        },
        rate: { mode: form.rate_mode, value: form.rate_value },
        call_limit: form.call_limit || undefined,
        max_calls: form.max_calls || undefined,
        duration: form.duration || undefined,
      };
      const res = await fleetApi.launch(req);
      const ok = res.dispatched.filter((d) => d.ok).length;
      const failed = res.dispatched.length - ok;
      if (ok === 0) {
        toast.error(`Launch failed on all ${res.dispatched.length} node(s).`);
      } else if (failed > 0) {
        toast.warn(`Launched on ${ok} node(s); ${failed} failed (partial).`);
      } else {
        toast.ok(`Campaign launched on ${ok} node(s) · run #${res.fleet_run_id}`);
      }
      onLaunched();
      setForm(LAUNCH_BLANK);
    } catch (e) {
      toast.error(`Launch failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      open={group !== null}
      title={<><IconPlay /> Launch on {group?.name}</>}
      onClose={onClose}
      footer={
        <ModalActions
          onCancel={onClose}
          onConfirm={launch}
          confirmLabel={submitting ? "Launching…" : "Launch"}
          disabled={submitting || onlineCount === 0}
        />
      }
    >
      <div className={s.notice}>
        <span className={s.noticeMark}>▸</span>
        <span>
          {onlineCount} online node(s) in <strong>{group?.name}</strong> will dial the destination.
          {onlineCount === 0 && " No online targets — bring a node online first."}
        </span>
      </div>

      <Field label="Campaign name" hint="Leave blank to auto-generate.">
        <input value={form.name} onChange={(e) => set("name", e.target.value)} placeholder="ny-soak" />
      </Field>
      <Field label="Scenario">
        <input
          value={form.scenario}
          onChange={(e) => set("scenario", e.target.value)}
          placeholder="basic_call"
        />
      </Field>

      <FieldRow>
        <Field label="Destination host">
          <input
            value={form.remote_host}
            onChange={(e) => set("remote_host", e.target.value)}
            placeholder="10.20.8.40"
          />
        </Field>
        <Field label="Port">
          <input
            type="number"
            value={form.remote_port}
            onChange={(e) => set("remote_port", Number(e.target.value))}
          />
        </Field>
        <Field label="Transport">
          <select
            value={form.transport}
            onChange={(e) => set("transport", e.target.value as Transport)}
          >
            <option value="udp">UDP</option>
            <option value="tcp">TCP</option>
            <option value="tls">TLS</option>
          </select>
        </Field>
      </FieldRow>

      <FieldRow>
        <Field label="Rate mode" hint="per_node = each node; total = split across nodes.">
          <select
            value={form.rate_mode}
            onChange={(e) => set("rate_mode", e.target.value as RateMode)}
          >
            <option value="per_node">Per node</option>
            <option value="total">Total (split)</option>
          </select>
        </Field>
        <Field
          label={form.rate_mode === "per_node" ? "CPS per node" : "Total CPS"}
        >
          <input
            type="number"
            value={form.rate_value}
            onChange={(e) => set("rate_value", Number(e.target.value))}
          />
        </Field>
        <Field label="Concurrent limit">
          <input
            type="number"
            value={form.call_limit}
            onChange={(e) => set("call_limit", Number(e.target.value))}
          />
        </Field>
      </FieldRow>

      <div className={s.notice} style={{ borderColor: "var(--line-phosphor)", background: "rgba(54,249,168,0.05)" }}>
        <span className={s.noticeMark} style={{ color: "var(--signal)" }}>
          ∑
        </span>
        <span>
          Effective load: <strong style={{ color: "var(--text-bright)" }}>{totalCps}</strong> cps total
          {onlineCount > 0 && <> · ~{perNodeCps} cps/node across {onlineCount} node(s)</>}
        </span>
      </div>

      <FieldRow>
        <Field label="Max calls" hint="0 = unlimited">
          <input
            type="number"
            value={form.max_calls}
            onChange={(e) => set("max_calls", Number(e.target.value))}
          />
        </Field>
        <Field label="Duration (s)" hint="0 = until stopped">
          <input
            type="number"
            value={form.duration}
            onChange={(e) => set("duration", Number(e.target.value))}
          />
        </Field>
      </FieldRow>
    </Modal>
  );
}
