import { useMemo, useState } from "react";
import s from "./pages.module.css";
import ui from "@/components/ui/ui.module.css";
import { Panel } from "@/components/ui/Panel";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Modal, ModalActions } from "@/components/ui/Modal";
import { Field, FieldRow, EmptyState, Spinner } from "@/components/ui/Misc";
import { IconPlug, IconPlus, IconTrash, IconRefresh } from "@/components/icons";
import { useAsync } from "@/hooks/useAsync";
import { api, getActiveBox, setActiveBox } from "@/lib/api";
import { useToast } from "@/components/ui/Toast";
import { ago, int } from "@/lib/format";
import type { FleetNode, Server, ServerRequest } from "@/lib/types";

/** Add/edit-node form state: a node = a source IP + its own number pool (origin +
 *  drop sale zone, optionally pinned to one code). "Each IP one loop". */
interface NodeForm {
  name: string;
  ip: string;
  description: string;
  groupId: string;
  originCountry: string;
  originZone: string;
  originCode: string;
  destCountry: string;
  destZone: string;
  destCode: string;
  count: number;
  regenerate: boolean; // edit mode: rebuild the pool with the zones/codes below
}

const BLANK: NodeForm = {
  name: "",
  ip: "",
  description: "",
  groupId: "",
  originCountry: "",
  originZone: "",
  originCode: "",
  destCountry: "",
  destZone: "",
  destCode: "",
  count: 500000,
  regenerate: false,
};

/**
 * Nodes = the source IPs this box originates loops from, each carrying its own
 * number pool (Country → Sale zone → Code). Add a node, pick its origin + drop
 * zones (and optionally a single routable code), and its A/B numbers are
 * generated here. Edit a node later to rename it, move it between groups, or
 * regenerate its numbers. The Loops "Run" form just picks a node or a group.
 */
export function Nodes() {
  const nodes = useAsync(() => api.listServers(), [], 4000);
  const detected = useAsync(() => api.sourceIps(), []);
  const zoneTree = useAsync(() => api.saleZones(), []);
  const groups = useAsync(() => api.listNodeGroups(), []);
  const toast = useToast();

  const [showForm, setShowForm] = useState(false);
  const [editId, setEditId] = useState<number | null>(null);
  const [form, setForm] = useState<NodeForm>(BLANK);
  const [busy, setBusy] = useState(false);
  const [regenId, setRegenId] = useState<number | null>(null);

  const set = <K extends keyof NodeForm>(k: K, v: NodeForm[K]) =>
    setForm((f) => ({ ...f, [k]: v }));

  const countries = useMemo(() => zoneTree.data?.countries ?? [], [zoneTree.data]);
  const zonesFor = (c: string): string[] =>
    countries.find((x) => x.name === c)?.zones ?? [];
  const codesFor = (z: string): string[] => zoneTree.data?.codes?.[z] ?? [];
  const countryOf = (zone: string): string =>
    countries.find((c) => c.zones.includes(zone))?.name ?? "";

  const openNew = () => {
    setEditId(null);
    setForm(BLANK);
    setShowForm(true);
  };

  const openEdit = (n: Server) => {
    setEditId(n.id);
    setForm({
      name: n.name,
      ip: n.ip,
      description: n.description,
      groupId: n.group_id != null ? String(n.group_id) : "",
      originCountry: countryOf(n.origin_zone),
      originZone: n.origin_zone,
      originCode: n.origin_code,
      destCountry: countryOf(n.dest_zone),
      destZone: n.dest_zone,
      destCode: n.dest_code,
      count: n.pool_count || 500000,
      regenerate: false,
    });
    setShowForm(true);
  };

  const save = async () => {
    if (!form.name.trim() || !form.ip.trim()) {
      toast.error("Name and IP are required.");
      return;
    }
    const creating = editId == null;
    if (creating && (!form.originZone || !form.destZone)) {
      toast.error("Pick an origin zone and a drop zone.");
      return;
    }
    if (!creating && form.regenerate && (!form.originZone || !form.destZone)) {
      toast.error("Pick an origin and drop zone to regenerate.");
      return;
    }
    setBusy(true);
    try {
      if (creating) {
        const res = await api.createServer({
          name: form.name,
          ip: form.ip,
          description: form.description,
          group_id: form.groupId ? Number(form.groupId) : null,
          origin_zone: form.originZone,
          dest_zone: form.destZone,
          origin_code: form.originCode,
          dest_code: form.destCode,
          count: form.count,
        } as ServerRequest);
        toast.ok(`Node added · ${res.server.name} · ${int(res.server.pool_count)} numbers`);
      } else {
        await api.updateServer(editId, {
          name: form.name,
          description: form.description,
          group_id: form.groupId ? Number(form.groupId) : -1, // -1 clears membership
        });
        if (form.regenerate) {
          const r = await api.generateServerPool(editId, {
            origin_zone: form.originZone,
            dest_zone: form.destZone,
            origin_code: form.originCode,
            dest_code: form.destCode,
            count: form.count,
          });
          toast.ok(`Node saved · ${int(r.server.pool_count)} numbers`);
        } else {
          toast.ok("Node saved");
        }
      }
      setShowForm(false);
      setForm(BLANK);
      setEditId(null);
      nodes.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  };

  const regen = async (id: number, name: string) => {
    setRegenId(id);
    try {
      const res = await api.generateServerPool(id, {});
      toast.ok(`Regenerated ${name} · ${int(res.server.pool_count)} numbers`);
      nodes.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    } finally {
      setRegenId(null);
    }
  };

  const del = async (id: number, name: string) => {
    try {
      await api.deleteServer(id);
      toast.warn(`Deleted ${name}`);
      nodes.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    }
  };

  const rows = nodes.data?.servers ?? [];
  const suggestions = detected.data?.source_ips ?? [];
  const groupName = (id: number | null) =>
    (groups.data?.groups ?? []).find((g) => g.id === id)?.name ?? "";

  return (
    <>
      <RemoteBoxes />

      <div className={s.toolbar}>
        <span className="hud-label">{rows.length} nodes</span>
        <div className={s.spacer} />
        <Button size="sm" variant="ghost" onClick={() => nodes.refetch()}>
          <IconRefresh /> Refresh
        </Button>
        <Button variant="primary" onClick={openNew}>
          <IconPlus /> Add Node
        </Button>
      </div>

      <Panel title="Origination Nodes (source IP + number pool)" flush>
        {nodes.loading && !nodes.data ? (
          <div style={{ padding: "var(--space-6)", display: "grid", placeItems: "center" }}>
            <Spinner />
          </div>
        ) : rows.length === 0 ? (
          <EmptyState
            title="No nodes yet"
            hint="Add a source IP and pick its origin + drop sale zones (and a routable code). Its numbers are generated here, then pick the node on the Loops Run form. One loop runs per IP."
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
                  <th>Name</th>
                  <th>Source IP</th>
                  <th>Group</th>
                  <th>Origin → Drop zone</th>
                  <th className={ui.numCell}>Pool</th>
                  <th>Added</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((n) => (
                  <tr key={n.id}>
                    <td style={{ color: "var(--text-bright)", fontWeight: 600 }}>{n.name}</td>
                    <td style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono, monospace)" }}>
                      {n.ip}
                    </td>
                    <td style={{ color: "var(--text-muted)" }}>
                      {n.group_id != null ? groupName(n.group_id) || `#${n.group_id}` : <span style={{ color: "var(--text-faint)" }}>—</span>}
                    </td>
                    <td style={{ color: "var(--text-muted)" }}>
                      {n.has_pool ? (
                        <>
                          {n.origin_zone}
                          {n.origin_code ? <span style={{ color: "var(--text-faint)" }}> ({n.origin_code})</span> : null}
                          <span style={{ color: "var(--text-faint)" }}> → </span>
                          {n.dest_zone}
                          {n.dest_code ? <span style={{ color: "var(--cyan)" }}> ({n.dest_code})</span> : null}
                        </>
                      ) : (
                        <span style={{ color: "var(--text-faint)" }}>— no pool —</span>
                      )}
                    </td>
                    <td className={ui.numCell}>
                      {n.has_pool ? (
                        <Badge tone="signal">{int(n.pool_count)}</Badge>
                      ) : (
                        <Badge tone="muted">none</Badge>
                      )}
                    </td>
                    <td style={{ color: "var(--text-muted)" }}>{ago(n.created_at)}</td>
                    <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                      <Button size="sm" variant="ghost" title="Edit node" onClick={() => openEdit(n)}>
                        Edit
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        title="Regenerate numbers"
                        disabled={!n.has_pool || regenId === n.id}
                        onClick={() => regen(n.id, n.name)}
                      >
                        <IconRefresh /> {regenId === n.id ? "…" : "Regen"}
                      </Button>
                      <Button size="sm" variant="ghost" icon title="Delete" onClick={() => del(n.id, n.name)}>
                        <IconTrash />
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <Modal
        open={showForm}
        title={<><IconPlug /> {editId != null ? "Edit node" : "Add Node"}</>}
        onClose={() => { setShowForm(false); setForm(BLANK); setEditId(null); }}
        footer={
          <ModalActions
            onCancel={() => { setShowForm(false); setForm(BLANK); setEditId(null); }}
            onConfirm={save}
            confirmLabel={busy ? "Saving…" : editId != null ? "Save" : "Add & generate"}
            disabled={busy}
          />
        }
      >
        <FieldRow>
          <Field label="Name">
            <input value={form.name} onChange={(e) => set("name", e.target.value)} placeholder="vandorial-1" />
          </Field>
          <Field
            label="Source IP"
            hint={editId != null ? "Fixed — delete + re-add to change." : suggestions.length ? "Or pick below." : "NIC address to bind."}
          >
            <input
              value={form.ip}
              disabled={editId != null}
              onChange={(e) => set("ip", e.target.value)}
              placeholder="10.20.8.11"
            />
          </Field>
          <Field label="Group" hint="Optional — group by route.">
            <select value={form.groupId} onChange={(e) => set("groupId", e.target.value)}>
              <option value="">No group</option>
              {(groups.data?.groups ?? []).map((g) => (
                <option key={g.id} value={g.id}>{g.name}</option>
              ))}
            </select>
          </Field>
        </FieldRow>
        {editId == null && suggestions.length > 0 && (
          <Field label="Detected on this box" hint="Click to use.">
            <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--space-2)" }}>
              {suggestions.map((ip) => (
                <Button key={ip} size="sm" variant="ghost" onClick={() => set("ip", ip)}>
                  {ip}
                </Button>
              ))}
            </div>
          </Field>
        )}

        <div className={s.formSection}>Numbers (Country → Sale zone → Code)</div>
        {editId != null && (
          <label
            style={{
              display: "flex",
              alignItems: "center",
              gap: 10,
              margin: "0 0 var(--space-3)",
              cursor: "pointer",
              fontSize: "var(--fs-sm)",
              color: "var(--text-muted)",
            }}
          >
            <input
              type="checkbox"
              checked={form.regenerate}
              onChange={(e) => set("regenerate", e.target.checked)}
              style={{ width: 18, height: 18, flex: "0 0 auto", margin: 0, accentColor: "var(--ember, #f26a21)" }}
            />
            <span>
              <strong style={{ color: "var(--text-bright)" }}>Regenerate numbers</strong> — rebuild this
              node's pool on save with the zones/codes below (e.g. pin <code>22462</code>).
            </span>
          </label>
        )}
        <FieldRow>
          <Field label="Origin country">
            <select
              value={form.originCountry}
              onChange={(e) => setForm((f) => ({ ...f, originCountry: e.target.value, originZone: "", originCode: "" }))}
            >
              <option value="">{zoneTree.loading ? "Loading…" : "Select country"}</option>
              {countries.map((c) => <option key={c.name} value={c.name}>{c.name}</option>)}
            </select>
          </Field>
          <Field label="Origin sale zone (A)">
            <select
              value={form.originZone}
              disabled={!form.originCountry}
              onChange={(e) => setForm((f) => ({ ...f, originZone: e.target.value, originCode: "" }))}
            >
              <option value="">Select zone</option>
              {zonesFor(form.originCountry).map((z) => <option key={z} value={z}>{z}</option>)}
            </select>
          </Field>
          <Field label="Origin code (A)" hint="All = spread the zone.">
            <select value={form.originCode} disabled={!form.originZone} onChange={(e) => set("originCode", e.target.value)}>
              <option value="">All codes</option>
              {codesFor(form.originZone).map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </Field>
        </FieldRow>
        <FieldRow>
          <Field label="Drop country">
            <select
              value={form.destCountry}
              onChange={(e) => setForm((f) => ({ ...f, destCountry: e.target.value, destZone: "", destCode: "" }))}
            >
              <option value="">{zoneTree.loading ? "Loading…" : "Select country"}</option>
              {countries.map((c) => <option key={c.name} value={c.name}>{c.name}</option>)}
            </select>
          </Field>
          <Field label="Drop sale zone (B)">
            <select
              value={form.destZone}
              disabled={!form.destCountry}
              onChange={(e) => setForm((f) => ({ ...f, destZone: e.target.value, destCode: "" }))}
            >
              <option value="">Select zone</option>
              {zonesFor(form.destCountry).map((z) => <option key={z} value={z}>{z}</option>)}
            </select>
          </Field>
          <Field label="Drop code (B)" hint="Pick the routable code (e.g. 22462).">
            <select value={form.destCode} disabled={!form.destZone} onChange={(e) => set("destCode", e.target.value)}>
              <option value="">All codes</option>
              {codesFor(form.destZone).map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </Field>
        </FieldRow>
        <Field label="How many" hint="Random draw pool (max 2,000,000).">
          <input
            type="number"
            min={1}
            max={2000000}
            value={form.count}
            onChange={(e) => set("count", Number(e.target.value) || 0)}
          />
        </Field>
        <Field label="Description" hint="Optional.">
          <input value={form.description} onChange={(e) => set("description", e.target.value)} />
        </Field>
      </Modal>
    </>
  );
}

/* ---- Boxes (this box + remote workers) -----------------------------------
   A box = a whole GenCall machine. "This box" is local; remote boxes are other
   workers you registered (address + their API key). Pick one to MANAGE — every
   page (incl. the source-IP nodes below) then drives that box via the proxy.
   Add/Test/Check/Remove all live here, on the Nodes page. */
function RemoteBoxes() {
  const boxes = useAsync(() => api.listFleetNodes(), [], 10000);
  const toast = useToast();
  const activeBox = getActiveBox();
  const [form, setForm] = useState({ name: "", address: "", api_key: "" });
  const [busy, setBusy] = useState(false);
  const [test, setTest] = useState<{ ok: boolean; msg: string } | null>(null);

  const list = boxes.data?.nodes ?? [];
  const setF = (k: "name" | "address" | "api_key", v: string) =>
    setForm((f) => ({ ...f, [k]: v }));

  // Switching box is a big context change — full reload so every page + the
  // topbar indicator reflect it cleanly (active box is persisted).
  const switchTo = (id: number | null) => {
    setActiveBox(id);
    window.location.reload();
  };

  const testConn = async () => {
    if (!form.address.trim()) {
      toast.error("Enter an address to test.");
      return;
    }
    setTest({ ok: false, msg: "testing…" });
    try {
      const r = await api.checkFleetNode(form.address, form.api_key);
      setTest(
        r.online
          ? { ok: true, msg: `online · v${r.version ?? "?"}` }
          : { ok: false, msg: `offline · ${r.error ?? "unreachable"}` },
      );
    } catch (e) {
      setTest({ ok: false, msg: `error: ${e instanceof Error ? e.message : e}` });
    }
  };

  const add = async () => {
    if (!form.name.trim() || !form.address.trim()) {
      toast.error("Name and address are required.");
      return;
    }
    setBusy(true);
    try {
      await api.createFleetNode({ name: form.name, address: form.address, api_key: form.api_key });
      toast.ok(`Added box ${form.name}`);
      setForm({ name: "", address: "", api_key: "" });
      setTest(null);
      boxes.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  };

  const del = async (b: FleetNode) => {
    try {
      await api.deleteFleetNode(b.id);
      if (getActiveBox() === b.id) setActiveBox(null);
      toast.warn(`Removed ${b.name}`);
      boxes.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    }
  };

  const managing = (on: boolean) =>
    on ? (
      <span style={{ marginLeft: 8, color: "var(--cyan)", fontSize: "var(--fs-2xs)", textTransform: "uppercase", letterSpacing: "var(--tracking-wide)" }}>
        ● managing
      </span>
    ) : null;

  return (
    <Panel title="Boxes (this box + remote workers)" flush>
      <div className={ui.tableWrap}>
        <table className={ui.table}>
          <thead>
            <tr><th>Box</th><th>Address</th><th>Status</th><th></th></tr>
          </thead>
          <tbody>
            <tr style={activeBox == null ? { background: "var(--bg-inset)" } : undefined}>
              <td style={{ color: "var(--text-bright)", fontWeight: 600 }}>
                This box (local){managing(activeBox == null)}
              </td>
              <td style={{ color: "var(--text-faint)", fontFamily: "var(--font-mono, monospace)" }}>127.0.0.1</td>
              <td><Badge tone="signal">online</Badge></td>
              <td style={{ textAlign: "right" }}>
                {activeBox != null && (
                  <Button size="sm" variant="ghost" onClick={() => switchTo(null)}>Switch here</Button>
                )}
              </td>
            </tr>
            {list.map((b) => (
              <tr key={b.id} style={activeBox === b.id ? { background: "var(--bg-inset)" } : undefined}>
                <td style={{ color: "var(--text-bright)", fontWeight: 600 }}>
                  {b.name}{managing(activeBox === b.id)}
                </td>
                <td style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono, monospace)" }}>{b.address}</td>
                <td><Badge tone={b.online ? "signal" : "crit"}>{b.online ? "online" : "offline"}</Badge></td>
                <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                  {activeBox !== b.id && (
                    <Button size="sm" variant="primary" disabled={!b.online} onClick={() => switchTo(b.id)}>
                      Manage
                    </Button>
                  )}
                  <Button size="sm" variant="ghost" title="Re-check" onClick={() => boxes.refetch()}>
                    <IconRefresh /> Check
                  </Button>
                  <Button size="sm" variant="ghost" icon title="Remove box" onClick={() => del(b)}>
                    <IconTrash />
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className={s.formSection}>Add a remote box</div>
      <FieldRow>
        <Field label="Name">
          <input value={form.name} onChange={(e) => setF("name", e.target.value)} placeholder="vandorial-3" />
        </Field>
        <Field label="Address" hint="host:port (http:// added)">
          <input value={form.address} onChange={(e) => setF("address", e.target.value)} placeholder="10.35.21.3:8000" />
        </Field>
        <Field label="API key" hint="that box's X-API-Key">
          <input type="password" value={form.api_key} onChange={(e) => setF("api_key", e.target.value)} placeholder="gc_…" autoComplete="off" />
        </Field>
      </FieldRow>
      <div style={{ display: "flex", gap: "var(--space-3)", alignItems: "center" }}>
        <Button size="sm" variant="ghost" onClick={testConn}>Test connection</Button>
        <Button size="sm" variant="primary" onClick={add} disabled={busy}>
          <IconPlus /> {busy ? "Adding…" : "Add box"}
        </Button>
        {test && (
          <span style={{ fontSize: "var(--fs-sm)", color: test.ok ? "var(--signal)" : "var(--crit)" }}>
            {test.msg}
          </span>
        )}
      </div>
    </Panel>
  );
}
