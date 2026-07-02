import { Outlet, useLocation } from "react-router-dom";
import { useState } from "react";
import styles from "./layout.module.css";
import { Sidebar } from "./Sidebar";
import { Button } from "../ui/Button";
import { IconPower } from "../icons";
import { useLiveStats, useStreamStatus } from "@/hooks/useStream";
import { api } from "@/lib/api";
import { useToast } from "../ui/Toast";
import { useAuth } from "@/lib/auth";
import { abbrev, num } from "@/lib/format";
import { Modal, ModalActions } from "../ui/Modal";

const TITLES: Record<string, { title: string; sub: string }> = {
  "/": { title: "Dashboard", sub: "Live traffic overview" },
  "/fleet": { title: "Fleet Overview", sub: "Cluster-wide telemetry" },
  "/nodes": { title: "Nodes", sub: "Boxes + origination source IPs (one loop per IP)" },
  "/groups": { title: "Groups", sub: "Membership & campaigns" },
  "/campaigns": { title: "Campaigns", sub: "Active & queued test runs" },
  "/scenarios": { title: "Scenarios", sub: "SIP message flows" },
  "/connectors": { title: "Connectors", sub: "SIP endpoints & trunks" },
  "/loops": { title: "Loops", sub: "Minutes-for-minutes loop campaigns" },
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
  const { canWrite } = useAuth();

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

            {canWrite && (
              <Button
                variant="danger"
                size="sm"
                onClick={() => setConfirmStop(true)}
                disabled={active === 0}
              >
                <IconPower /> Stop All
              </Button>
            )}
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
