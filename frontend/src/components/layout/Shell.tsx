import { Outlet, useLocation } from "react-router-dom";
import { useState } from "react";
import styles from "./layout.module.css";
import fleet from "@/pages/fleet.module.css";
import { Sidebar } from "./Sidebar";
import { Button } from "../ui/Button";
import { IconPower } from "../icons";
import { useLiveStats, useStreamStatus } from "@/hooks/useStream";
import { api, getActiveBox, setActiveBox } from "@/lib/api";
import { useToast } from "../ui/Toast";
import { abbrev, num } from "@/lib/format";
import { Modal, ModalActions } from "../ui/Modal";
import { useAsync } from "@/hooks/useAsync";

const TITLES: Record<string, { title: string; sub: string }> = {
  "/": { title: "Dashboard", sub: "Live traffic overview" },
  "/fleet": { title: "Fleet Overview", sub: "Cluster-wide telemetry" },
  "/nodes": { title: "Nodes", sub: "Boxes + origination source IPs (one loop per IP)" },
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

          <BoxSwitch />

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
          <div className={styles.contentInner} key={loc.pathname}>
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

/* ---- Quick box switch (manage boxes on the Nodes page) ------------------
   Pick "This box" or a registered remote box. Switching routes every
   worker-facing call through that box's proxy; we full-reload so every page +
   this indicator reflect the new box cleanly. Add/check/remove boxes lives on
   the Nodes page. */
function BoxSwitch() {
  const boxes = useAsync(() => api.listFleetNodes(), [], 15000);
  const active = getActiveBox();
  const list = boxes.data?.nodes ?? [];

  const choose = (id: number | null) => {
    setActiveBox(id);
    window.location.reload();
  };

  return (
    <div className={fleet.scopeBar} title="Managed box — add/remove on the Nodes page">
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
    </div>
  );
}
