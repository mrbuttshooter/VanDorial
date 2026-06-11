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
import { api } from "@/lib/api";
import { useToast } from "@/components/ui/Toast";
import { ago, int } from "@/lib/format";
import type { ServerRequest } from "@/lib/types";

/** Add-node form state: a node = a source IP + its own number pool (origin +
 *  drop sale zone). "Each IP one loop", so the node IS the loop unit. */
interface NodeForm {
  name: string;
  ip: string;
  description: string;
  originCountry: string;
  originZone: string;
  destCountry: string;
  destZone: string;
  count: number;
}

const BLANK: NodeForm = {
  name: "",
  ip: "",
  description: "",
  originCountry: "",
  originZone: "",
  destCountry: "",
  destZone: "",
  count: 500000,
};

/**
 * Nodes = the source IPs this box originates loops from, each carrying its own
 * number pool (one loop per IP). Add a node, pick its origin + drop sale zones,
 * and its A/B numbers are generated here — then the New Loop form just picks a
 * node. (On a single box these are NIC addresses; the same record extends to
 * fleet nodes later.)
 */
export function Nodes() {
  const nodes = useAsync(() => api.listServers(), [], 4000);
  const detected = useAsync(() => api.sourceIps(), []);
  const zoneTree = useAsync(() => api.saleZones(), []);
  const toast = useToast();

  const [showNew, setShowNew] = useState(false);
  const [form, setForm] = useState<NodeForm>(BLANK);
  const [busy, setBusy] = useState(false);
  const [regenId, setRegenId] = useState<number | null>(null);

  const set = <K extends keyof NodeForm>(k: K, v: NodeForm[K]) =>
    setForm((f) => ({ ...f, [k]: v }));

  const countries = useMemo(() => zoneTree.data?.countries ?? [], [zoneTree.data]);
  const zonesFor = (c: string): string[] =>
    countries.find((x) => x.name === c)?.zones ?? [];

  const create = async () => {
    if (!form.name.trim() || !form.ip.trim()) {
      toast.error("Name and IP are required.");
      return;
    }
    if (!form.originZone || !form.destZone) {
      toast.error("Pick an origin zone and a drop zone.");
      return;
    }
    setBusy(true);
    try {
      const res = await api.createServer({
        name: form.name,
        ip: form.ip,
        description: form.description,
        origin_zone: form.originZone,
        dest_zone: form.destZone,
        count: form.count,
      } as ServerRequest);
      toast.ok(
        `Node added · ${res.server.name} · ${int(res.server.pool_count)} numbers`,
      );
      setShowNew(false);
      setForm(BLANK);
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

  return (
    <>
      <div className={s.toolbar}>
        <span className="hud-label">{rows.length} nodes</span>
        <div className={s.spacer} />
        <Button size="sm" variant="ghost" onClick={() => nodes.refetch()}>
          <IconRefresh /> Refresh
        </Button>
        <Button variant="primary" onClick={() => setShowNew(true)}>
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
            hint="Add a source IP and pick its origin + drop sale zones. Its numbers are generated here, then pick the node on the New Loop form. One loop runs per IP."
            action={
              <Button variant="primary" size="sm" onClick={() => setShowNew(true)}>
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
                      {n.has_pool ? (
                        <>{n.origin_zone} <span style={{ color: "var(--text-faint)" }}>→</span> {n.dest_zone}</>
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
        open={showNew}
        title={<><IconPlug /> Add Node</>}
        onClose={() => { setShowNew(false); setForm(BLANK); }}
        footer={
          <ModalActions
            onCancel={() => { setShowNew(false); setForm(BLANK); }}
            onConfirm={create}
            confirmLabel={busy ? "Generating…" : "Add & generate"}
            disabled={busy}
          />
        }
      >
        <FieldRow>
          <Field label="Name">
            <input value={form.name} onChange={(e) => set("name", e.target.value)} placeholder="vandorial-1" />
          </Field>
          <Field label="Source IP" hint={suggestions.length ? "Or pick below." : "NIC address to bind."}>
            <input value={form.ip} onChange={(e) => set("ip", e.target.value)} placeholder="10.20.8.11" />
          </Field>
        </FieldRow>
        {suggestions.length > 0 && (
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

        <div className={s.formSection}>Numbers (drop zones)</div>
        <FieldRow>
          <Field label="Origin country">
            <select
              value={form.originCountry}
              onChange={(e) => setForm((f) => ({ ...f, originCountry: e.target.value, originZone: "" }))}
            >
              <option value="">{zoneTree.loading ? "Loading…" : "Select country"}</option>
              {countries.map((c) => <option key={c.name} value={c.name}>{c.name}</option>)}
            </select>
          </Field>
          <Field label="Origin sale zone (A)">
            <select value={form.originZone} disabled={!form.originCountry} onChange={(e) => set("originZone", e.target.value)}>
              <option value="">Select zone</option>
              {zonesFor(form.originCountry).map((z) => <option key={z} value={z}>{z}</option>)}
            </select>
          </Field>
        </FieldRow>
        <FieldRow>
          <Field label="Drop country">
            <select
              value={form.destCountry}
              onChange={(e) => setForm((f) => ({ ...f, destCountry: e.target.value, destZone: "" }))}
            >
              <option value="">{zoneTree.loading ? "Loading…" : "Select country"}</option>
              {countries.map((c) => <option key={c.name} value={c.name}>{c.name}</option>)}
            </select>
          </Field>
          <Field label="Drop sale zone (B)">
            <select value={form.destZone} disabled={!form.destCountry} onChange={(e) => set("destZone", e.target.value)}>
              <option value="">Select zone</option>
              {zonesFor(form.destCountry).map((z) => <option key={z} value={z}>{z}</option>)}
            </select>
          </Field>
          <Field label="How many" hint="Random draw pool (max 2,000,000).">
            <input
              type="number"
              min={1}
              max={2000000}
              value={form.count}
              onChange={(e) => set("count", Number(e.target.value) || 0)}
            />
          </Field>
        </FieldRow>
        <Field label="Description" hint="Optional.">
          <input value={form.description} onChange={(e) => set("description", e.target.value)} />
        </Field>
      </Modal>
    </>
  );
}
