import { useState } from "react";
import s from "./pages.module.css";
import ui from "@/components/ui/ui.module.css";
import { Panel } from "@/components/ui/Panel";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Modal, ModalActions } from "@/components/ui/Modal";
import { Field, FieldRow, EmptyState, Spinner } from "@/components/ui/Misc";
import { IconPlug, IconPlus, IconTrash } from "@/components/icons";
import { useAsync } from "@/hooks/useAsync";
import { api } from "@/lib/api";
import { useToast } from "@/components/ui/Toast";
import { ago } from "@/lib/format";
import type { ServerRequest } from "@/lib/types";

const BLANK: ServerRequest = { name: "", ip: "", description: "" };

/**
 * Servers = the source IPs a loop can originate from ("Node = IP"). Add the
 * box's NIC addresses here (auto-detected ones are suggested), then pick a
 * server from the dropdown on the New Loop form. One loop runs per IP.
 */
export function Servers() {
  const list = useAsync(() => api.listServers(), []);
  const detected = useAsync(() => api.sourceIps(), []);
  const toast = useToast();
  const [showNew, setShowNew] = useState(false);
  const [form, setForm] = useState<ServerRequest>(BLANK);

  const set = <K extends keyof ServerRequest>(k: K, v: ServerRequest[K]) =>
    setForm((f) => ({ ...f, [k]: v }));

  const create = async () => {
    if (!form.name.trim() || !form.ip.trim()) {
      toast.error("Name and IP are required.");
      return;
    }
    try {
      await api.createServer(form);
      toast.ok(`Server added · ${form.name}`);
      setShowNew(false);
      setForm(BLANK);
      list.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    }
  };

  const del = async (id: number, name: string) => {
    try {
      await api.deleteServer(id);
      toast.warn(`Deleted ${name}`);
      list.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    }
  };

  const rows = list.data?.servers ?? [];
  const suggestions = detected.data?.source_ips ?? [];

  return (
    <>
      <div className={s.toolbar}>
        <span className="hud-label">{rows.length} servers</span>
        <div className={s.spacer} />
        <Button variant="primary" onClick={() => setShowNew(true)}>
          <IconPlus /> Add Server
        </Button>
      </div>

      <Panel title="Origination Servers (source IPs)" flush>
        {list.loading && !list.data ? (
          <div style={{ padding: "var(--space-6)", display: "grid", placeItems: "center" }}>
            <Spinner />
          </div>
        ) : rows.length === 0 ? (
          <EmptyState
            title="No servers yet"
            hint="Add the source IPs this box originates from. Each loop binds to one, and one loop runs per IP."
            action={
              <Button variant="primary" size="sm" onClick={() => setShowNew(true)}>
                Add server
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
                  <th>Description</th>
                  <th>State</th>
                  <th>Added</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((sv) => (
                  <tr key={sv.id}>
                    <td style={{ color: "var(--text-bright)", fontWeight: 600 }}>{sv.name}</td>
                    <td style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono, monospace)" }}>
                      {sv.ip}
                    </td>
                    <td style={{ color: "var(--text-faint)" }}>{sv.description || "—"}</td>
                    <td>
                      <Badge tone={sv.enabled ? "signal" : "muted"}>
                        {sv.enabled ? "enabled" : "disabled"}
                      </Badge>
                    </td>
                    <td style={{ color: "var(--text-muted)" }}>{ago(sv.created_at)}</td>
                    <td style={{ textAlign: "right" }}>
                      <Button size="sm" variant="ghost" icon title="Delete" onClick={() => del(sv.id, sv.name)}>
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
        title={<><IconPlug /> Add Server</>}
        onClose={() => { setShowNew(false); setForm(BLANK); }}
        footer={<ModalActions onCancel={() => { setShowNew(false); setForm(BLANK); }} onConfirm={create} confirmLabel="Add" />}
      >
        <FieldRow>
          <Field label="Name">
            <input value={form.name} onChange={(e) => set("name", e.target.value)} placeholder="vandorial-1" />
          </Field>
          <Field
            label="Source IP"
            hint={suggestions.length ? "Or pick a detected IP below." : "The NIC address to bind."}
          >
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
        <Field label="Description" hint="Optional.">
          <input value={form.description} onChange={(e) => set("description", e.target.value)} />
        </Field>
      </Modal>
    </>
  );
}
