import { Outlet, useLocation } from "react-router-dom";
import { useState } from "react";
import styles from "./layout.module.css";
import fleet from "@/pages/fleet.module.css";
import { Sidebar } from "./Sidebar";
import { Button } from "../ui/Button";
import { Badge } from "../ui/Badge";
import { Field, FieldRow } from "../ui/Misc";
import { IconPower, IconPlus, IconTrash } from "../icons";
import { useLiveStats, useStreamStatus } from "@/hooks/useStream";
import { api, getActiveBox, setActiveBox } from "@/lib/api";
import { useToast } from "../ui/Toast";
import { abbrev, num } from "@/lib/format";
import { Modal, ModalActions } from "../ui/Modal";
import { useAsync } from "@/hooks/useAsync";
import type { FleetNode } from "@/lib/types";

const TITLES: Record<string, { title: string; sub: string }> = {
  "/": { title: "Dashboard", sub: "Live traffic overview" },
  "/fleet": { title: "Fleet Overview", sub: "Cluster-wide telemetry" },
  "/nodes": { title: "Nodes", sub: "Origination source IPs + number pools (one loop per IP)" },
  "/groups": { title: "Groups", sub: "Membership & campaigns" },
  "/campaigns": { title: "Campaigns", sub: "Active & queued test runs" },
  "/scenarios": { title: "Scenarios", sub: "SIP message flows" },
  "/connectors": { title: "Connectors", sub: "SIP endpoints & trunks" },
  "/loops": { title: "Loops", sub: "Minutes-for-minutes loop campaigns" },
  "/console": { title: "Console", sub: "Live event stream" },
  "/performance": { title: "Performance", sub: "Throughput & latency telemetry" },
  "/history": { title: "History", sub: "Completed run archive" },
  "/config": { title: "Configuration", sub: "Server & runtime settings" },
};

export function Shell() {
  const loc = useLocation();
  const meta = TITLES[loc.pathname] ?? { title: "GenCall", sub: "" };
  const { latest } = useLiveStats(60);
  const connected = useStreamStatus();
  const toast = useToast();
  const [confirmStop, setConfirmStop] = useState(false);
  // Bumped on box switch so the whole page area remounts and refetches against
  // the newly-selected box (through the proxy).
  const [boxKey, setBoxKey] = useState(0);

  const active = latest?.active_instances ?? 0;

  const stopAll = async () => {
    setConfirmStop(false);
    try {
      await api.stopAll();
      toast.warn("Emergency stop issued — all tests halting.");
    } catch (e) {
      toast.error(`Stop-all failed: ${e instanceof Error ? e.message : e}`);
    }
  };

  return (
    <div className={styles.shell}>
      <Sidebar activeTests={active} />
      <div className={styles.main}>
        <header className={styles.topbar}>
          <div className={styles.crumb}>
            <span className={styles.crumbTitle}>{meta.title}</span>
            <span className={styles.crumbSub}>/ {meta.sub}</span>
          </div>

          <BoxSelector onSwitch={() => setBoxKey((k) => k + 1)} />

          <div className={styles.topMeters}>
            <div className={styles.meter}>
              <span className={styles.meterVal}>{active}</span>
              <span className={styles.meterLabel}>Active</span>
            </div>
            <div className={styles.meter}>
              <span className={styles.meterVal}>{num(latest?.calls_per_second ?? 0, 1)}</span>
              <span className={styles.meterLabel}>CPS</span>
            </div>
            <div className={styles.meter}>
              <span className={styles.meterVal}>{abbrev(latest?.total_calls ?? 0)}</span>
              <span className={styles.meterLabel}>Calls</span>
            </div>

            <span
              className={styles.conn}
              style={{ color: connected ? "var(--signal)" : "var(--crit)" }}
            >
              <span
                style={{
                  width: 6,
                  height: 6,
                  borderRadius: "50%",
                  background: "currentColor",
                  boxShadow: "0 0 8px currentColor",
                }}
              />
              {connected ? "Stream Live" : "Reconnecting"}
            </span>

            <Button
              variant="danger"
              size="sm"
              onClick={() => setConfirmStop(true)}
              disabled={active === 0}
            >
              <IconPower /> Stop All
            </Button>
          </div>
        </header>

        <main className={styles.content}>
          <div className={styles.contentInner} key={`${boxKey}:${loc.pathname}`}>
            <Outlet />
          </div>
        </main>
      </div>

      <Modal
        open={confirmStop}
        title="Emergency Stop"
        onClose={() => setConfirmStop(false)}
        footer={
          <ModalActions
            onCancel={() => setConfirmStop(false)}
            onConfirm={stopAll}
            confirmLabel="Stop everything"
            danger
          />
        }
      >
        <p style={{ color: "var(--text)" }}>
          Halt all <strong style={{ color: "var(--crit)" }}>{active}</strong> running test
          {active === 1 ? "" : "s"} immediately. In-flight calls will be torn down.
        </p>
      </Modal>
    </div>
  );
}

/* ---- Box selector (one-GUI control plane) ------------------------------
   Pick "This box" (local) or a registered remote worker. Selecting a box routes
   every worker-facing call through that box's proxy (see api.scopedPath), so the
   same Nodes/Loops/Presets/History pages manage whichever box is selected. */
function BoxSelector({ onSwitch }: { onSwitch: () => void }) {
  const boxes = useAsync(() => api.listFleetNodes(), [], 15000);
  const [active, setActive] = useState<number | null>(getActiveBox());
  const [manage, setManage] = useState(false);

  const list = boxes.data?.nodes ?? [];

  const choose = (id: number | null) => {
    setActiveBox(id);
    setActive(id);
    onSwitch();
  };

  return (
    <div className={fleet.scopeBar} title="Managed box">
      <span className="hud-label">Box</span>
      <select
        className={fleet.scopeSelect}
        value={active ?? ""}
        onChange={(e) => choose(e.target.value ? Number(e.target.value) : null)}
        aria-label="Managed box"
      >
        <option value="">This box (local)</option>
        {list.map((b) => (
          <option key={b.id} value={b.id}>
            {b.name}
            {b.online === false ? " · offline" : ""}
          </option>
        ))}
      </select>
      {active != null && (
        <span
          title="Driving a remote box"
          style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--cyan)", boxShadow: "0 0 8px var(--cyan)" }}
        />
      )}
      <button
        className={fleet.scopeSelect}
        style={{ cursor: "pointer", padding: "0 8px" }}
        onClick={() => setManage(true)}
        title="Add / remove boxes"
      >
        ⚙
      </button>
      {manage && (
        <ManageBoxes
          onClose={() => {
            setManage(false);
            boxes.refetch();
          }}
        />
      )}
    </div>
  );
}

function ManageBoxes({ onClose }: { onClose: () => void }) {
  const boxes = useAsync(() => api.listFleetNodes(), [], 8000);
  const toast = useToast();
  const [form, setForm] = useState({ name: "", address: "", api_key: "" });
  const [busy, setBusy] = useState(false);

  const list = boxes.data?.nodes ?? [];
  const set = (k: keyof typeof form, v: string) => setForm((f) => ({ ...f, [k]: v }));

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

  return (
    <Modal
      open
      title="Boxes — remote workers"
      onClose={onClose}
      footer={<ModalActions onCancel={onClose} onConfirm={onClose} confirmLabel="Done" />}
    >
      <FieldRow>
        <Field label="Name">
          <input value={form.name} onChange={(e) => set("name", e.target.value)} placeholder="vandorial-3" />
        </Field>
        <Field label="Address" hint="host:port (http:// added)">
          <input value={form.address} onChange={(e) => set("address", e.target.value)} placeholder="10.35.21.3:8000" />
        </Field>
      </FieldRow>
      <Field label="API key" hint="that box's X-API-Key">
        <input type="password" value={form.api_key} onChange={(e) => set("api_key", e.target.value)} placeholder="gc_…" autoComplete="off" />
      </Field>
      <div style={{ margin: "var(--space-3) 0" }}>
        <Button variant="primary" size="sm" onClick={add} disabled={busy}>
          <IconPlus /> {busy ? "Adding…" : "Add box"}
        </Button>
      </div>

      {list.length === 0 ? (
        <p style={{ fontSize: "var(--fs-sm)", color: "var(--text-muted)" }}>
          No remote boxes yet. Add one above, then pick it in the topbar to manage it from here.
        </p>
      ) : (
        <div style={{ display: "grid", gap: 6 }}>
          {list.map((b) => (
            <div
              key={b.id}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 10,
                padding: "6px 10px",
                border: "1px solid var(--line)",
                borderRadius: "var(--r-sm)",
                background: "var(--bg-inset)",
              }}
            >
              <Badge tone={b.online ? "signal" : "muted"}>{b.online ? "online" : "offline"}</Badge>
              <span style={{ color: "var(--text-bright)", fontWeight: 600 }}>{b.name}</span>
              <span style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono, monospace)", fontSize: "var(--fs-xs)" }}>
                {b.address}
              </span>
              <div style={{ flex: 1 }} />
              <Button size="sm" variant="ghost" icon title="Remove box" onClick={() => del(b)}>
                <IconTrash />
              </Button>
            </div>
          ))}
        </div>
      )}
    </Modal>
  );
}
