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
import type { ConnectorRequest, Transport } from "@/lib/types";

const BLANK: ConnectorRequest = {
  name: "",
  description: "",
  local_ip: "0.0.0.0",
  local_port: 5060,
  remote_ip: "",
  remote_port: 5060,
  transport: "udp",
  auth_user: "",
  auth_pass: "",
};

export function Connectors() {
  const list = useAsync(() => api.listConnectors(), []);
  const toast = useToast();
  const [showNew, setShowNew] = useState(false);
  const [form, setForm] = useState<ConnectorRequest>(BLANK);

  const set = <K extends keyof ConnectorRequest>(k: K, v: ConnectorRequest[K]) =>
    setForm((f) => ({ ...f, [k]: v }));

  const create = async () => {
    if (!form.name.trim() || !form.remote_ip.trim()) {
      toast.error("Name and remote IP are required.");
      return;
    }
    try {
      await api.createConnector(form);
      toast.ok(`Connector created · ${form.name}`);
      setShowNew(false);
      setForm(BLANK);
      list.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    }
  };

  const del = async (name: string) => {
    try {
      await api.deleteConnector(name);
      toast.warn(`Deleted ${name}`);
      list.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    }
  };

  const rows = list.data?.connectors ?? [];

  return (
    <>
      <div className={s.toolbar}>
        <span className="hud-label">{rows.length} connectors</span>
        <div className={s.spacer} />
        <Button variant="primary" onClick={() => setShowNew(true)}>
          <IconPlus /> New Connector
        </Button>
      </div>

      <Panel title="SIP Endpoints" flush>
        {list.loading && !list.data ? (
          <div style={{ padding: "var(--space-6)", display: "grid", placeItems: "center" }}>
            <Spinner />
          </div>
        ) : rows.length === 0 ? (
          <EmptyState
            title="No connectors"
            hint="Define SIP endpoints (SBCs, trunks, registrars) you test against."
            action={
              <Button variant="primary" size="sm" onClick={() => setShowNew(true)}>
                Add connector
              </Button>
            }
          />
        ) : (
          <div className={ui.tableWrap}>
            <table className={ui.table}>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Local</th>
                  <th>Remote</th>
                  <th>Transport</th>
                  <th>Auth</th>
                  <th>State</th>
                  <th>Updated</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((c) => (
                  <tr key={c.id}>
                    <td>
                      <div style={{ color: "var(--text-bright)", fontWeight: 600 }}>{c.name}</div>
                      {c.description && (
                        <div style={{ fontSize: "var(--fs-2xs)", color: "var(--text-faint)" }}>
                          {c.description}
                        </div>
                      )}
                    </td>
                    <td style={{ color: "var(--text-muted)" }}>
                      {c.local_ip}:{c.local_port}
                    </td>
                    <td style={{ color: "var(--text-muted)" }}>
                      {c.remote_ip}:{c.remote_port}
                    </td>
                    <td style={{ textTransform: "uppercase" }}>{c.transport}</td>
                    <td style={{ color: c.auth_user ? "var(--text)" : "var(--text-faint)" }}>
                      {c.auth_user || "—"}
                    </td>
                    <td>
                      <Badge tone={c.enabled ? "signal" : "muted"}>
                        {c.enabled ? "enabled" : "disabled"}
                      </Badge>
                    </td>
                    <td style={{ color: "var(--text-muted)" }}>{ago(c.updated_at)}</td>
                    <td style={{ textAlign: "right" }}>
                      <Button size="sm" variant="ghost" icon title="Delete" onClick={() => del(c.name)}>
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
        title={<><IconPlug /> New Connector</>}
        onClose={() => setShowNew(false)}
        footer={<ModalActions onCancel={() => setShowNew(false)} onConfirm={create} confirmLabel="Create" />}
      >
        <FieldRow>
          <Field label="Name">
            <input value={form.name} onChange={(e) => set("name", e.target.value)} placeholder="lab-sbc-edge" />
          </Field>
          <Field label="Transport">
            <select value={form.transport} onChange={(e) => set("transport", e.target.value as Transport)}>
              <option value="udp">UDP</option>
              <option value="tcp">TCP</option>
              <option value="tls">TLS</option>
            </select>
          </Field>
        </FieldRow>
        <Field label="Description">
          <input value={form.description} onChange={(e) => set("description", e.target.value)} />
        </Field>
        <FieldRow>
          <Field label="Local IP">
            <input value={form.local_ip} onChange={(e) => set("local_ip", e.target.value)} />
          </Field>
          <Field label="Local port">
            <input type="number" value={form.local_port} onChange={(e) => set("local_port", Number(e.target.value))} />
          </Field>
        </FieldRow>
        <FieldRow>
          <Field label="Remote IP">
            <input value={form.remote_ip} onChange={(e) => set("remote_ip", e.target.value)} placeholder="10.20.8.40" />
          </Field>
          <Field label="Remote port">
            <input type="number" value={form.remote_port} onChange={(e) => set("remote_port", Number(e.target.value))} />
          </Field>
        </FieldRow>
        <FieldRow>
          <Field label="Auth user" hint="Optional digest credentials">
            <input value={form.auth_user} onChange={(e) => set("auth_user", e.target.value)} />
          </Field>
          <Field label="Auth password">
            <input type="password" value={form.auth_pass} onChange={(e) => set("auth_pass", e.target.value)} />
          </Field>
        </FieldRow>
      </Modal>
    </>
  );
}
