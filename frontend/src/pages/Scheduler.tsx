import { useState } from "react";
import s from "./pages.module.css";
import ui from "@/components/ui/ui.module.css";
import { Panel } from "@/components/ui/Panel";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Modal, ModalActions } from "@/components/ui/Modal";
import { Field, FieldRow, EmptyState } from "@/components/ui/Misc";
import { IconClock, IconPlus, IconTrash } from "@/components/icons";
import { useToast } from "@/components/ui/Toast";

/**
 * The engine ships a full job scheduler (gencall/core/scheduler.py) but it is
 * not yet exposed through the REST API. This page is a working UI shell with
 * local state — once a /api/scheduler route lands, swap local state for it.
 */
interface Job {
  id: number;
  name: string;
  scenario: string;
  cron: string;
  enabled: boolean;
}

const SEED: Job[] = [
  { id: 1, name: "nightly-soak", scenario: "basic_call", cron: "0 2 * * *", enabled: true },
  { id: 2, name: "hourly-options-ping", scenario: "options_ping", cron: "0 * * * *", enabled: true },
  { id: 3, name: "weekly-capacity", scenario: "stress_test", cron: "0 4 * * 0", enabled: false },
];

export function Scheduler() {
  const toast = useToast();
  const [jobs, setJobs] = useState<Job[]>(SEED);
  const [showNew, setShowNew] = useState(false);
  const [draft, setDraft] = useState({ name: "", scenario: "basic_call", cron: "0 * * * *" });

  const add = () => {
    if (!draft.name.trim()) {
      toast.error("Job name required.");
      return;
    }
    setJobs((j) => [...j, { id: Date.now(), ...draft, enabled: true }]);
    toast.ok(`Scheduled · ${draft.name}`);
    setShowNew(false);
    setDraft({ name: "", scenario: "basic_call", cron: "0 * * * *" });
  };

  const toggle = (id: number) =>
    setJobs((j) => j.map((x) => (x.id === id ? { ...x, enabled: !x.enabled } : x)));
  const remove = (id: number) => setJobs((j) => j.filter((x) => x.id !== id));

  return (
    <>
      <div className={s.notice}>
        <span className={s.noticeMark}>▲</span>
        <span>
          Preview — the scheduler engine exists in <code>core/scheduler.py</code> but has no REST
          route yet. Jobs below are held in the browser. Wire <code>/api/scheduler</code> to persist
          them server-side.
        </span>
      </div>

      <div className={s.toolbar}>
        <span className="hud-label">{jobs.length} jobs</span>
        <div className={s.spacer} />
        <Button variant="primary" onClick={() => setShowNew(true)}>
          <IconPlus /> New Job
        </Button>
      </div>

      <Panel title="Scheduled Jobs" flush>
        {jobs.length === 0 ? (
          <EmptyState title="No scheduled jobs" hint="Add a recurring test on a cron schedule." />
        ) : (
          <div className={ui.tableWrap}>
            <table className={ui.table}>
              <thead>
                <tr>
                  <th>Job</th>
                  <th>Scenario</th>
                  <th>Cron</th>
                  <th>State</th>
                  <th style={{ textAlign: "right" }}>Actions</th>
                </tr>
              </thead>
              <tbody>
                {jobs.map((j) => (
                  <tr key={j.id}>
                    <td style={{ color: "var(--text-bright)", fontWeight: 600 }}>{j.name}</td>
                    <td style={{ color: "var(--text-muted)" }}>{j.scenario}</td>
                    <td style={{ fontFamily: "var(--font-mono)", color: "var(--cyan)" }}>{j.cron}</td>
                    <td>
                      <Badge tone={j.enabled ? "signal" : "muted"}>
                        {j.enabled ? "armed" : "paused"}
                      </Badge>
                    </td>
                    <td style={{ textAlign: "right" }}>
                      <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
                        <Button size="sm" variant="ghost" onClick={() => toggle(j.id)}>
                          {j.enabled ? "Pause" : "Arm"}
                        </Button>
                        <Button size="sm" variant="ghost" icon title="Delete" onClick={() => remove(j.id)}>
                          <IconTrash />
                        </Button>
                      </div>
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
        title={<><IconClock /> New Scheduled Job</>}
        onClose={() => setShowNew(false)}
        footer={<ModalActions onCancel={() => setShowNew(false)} onConfirm={add} confirmLabel="Schedule" />}
      >
        <Field label="Job name">
          <input value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder="nightly-soak" />
        </Field>
        <FieldRow>
          <Field label="Scenario">
            <input value={draft.scenario} onChange={(e) => setDraft({ ...draft, scenario: e.target.value })} />
          </Field>
          <Field label="Cron expression" hint="min hour dom mon dow">
            <input value={draft.cron} onChange={(e) => setDraft({ ...draft, cron: e.target.value })} />
          </Field>
        </FieldRow>
      </Modal>
    </>
  );
}
