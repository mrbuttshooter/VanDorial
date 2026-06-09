import { useState } from "react";
import s from "./pages.module.css";
import ui from "@/components/ui/ui.module.css";
import { Panel } from "@/components/ui/Panel";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { statusTone } from "@/components/ui/tone";
import { Modal, ModalActions } from "@/components/ui/Modal";
import { Field, FieldRow, EmptyState, Spinner } from "@/components/ui/Misc";
import { IconPlay, IconStop, IconTrash, IconSliders, IconPlus, IconRefresh } from "@/components/icons";
import { useAsync } from "@/hooks/useAsync";
import { api } from "@/lib/api";
import { useToast } from "@/components/ui/Toast";
import { duration, int, num, pct } from "@/lib/format";
import type { StartTestRequest, Transport } from "@/lib/types";

const BLANK: StartTestRequest = {
  name: "",
  scenario: "basic_call",
  remote_host: "",
  remote_port: 5060,
  transport: "udp",
  call_rate: 10,
  call_limit: 20,
  max_calls: 0,
  duration: 0,
};

export function Campaigns() {
  const tests = useAsync(() => api.listTests(), [], 2000);
  const scenarios = useAsync(() => api.listScenarios(), []);
  const toast = useToast();

  const [showNew, setShowNew] = useState(false);
  const [form, setForm] = useState<StartTestRequest>(BLANK);
  const [rateFor, setRateFor] = useState<string | null>(null);
  const [rate, setRate] = useState(10);

  const set = <K extends keyof StartTestRequest>(k: K, v: StartTestRequest[K]) =>
    setForm((f) => ({ ...f, [k]: v }));

  const launch = async () => {
    if (!form.remote_host.trim()) {
      toast.error("Remote host is required.");
      return;
    }
    try {
      const res = await api.startTest(form);
      toast.ok(`Campaign launched · ${res.id}`);
      setShowNew(false);
      setForm(BLANK);
      tests.refetch();
    } catch (e) {
      toast.error(`Launch failed: ${e instanceof Error ? e.message : e}`);
    }
  };

  const stop = async (id: string) => {
    try {
      await api.stopTest(id);
      toast.warn(`Stopped ${id}`);
      tests.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    }
  };

  const remove = async (id: string) => {
    try {
      await api.removeTest(id);
      tests.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    }
  };

  const applyRate = async () => {
    if (!rateFor) return;
    try {
      await api.updateRate(rateFor, rate);
      toast.ok(`Rate → ${rate} cps on ${rateFor}`);
      setRateFor(null);
      tests.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    }
  };

  const rows = tests.data?.tests ?? [];

  return (
    <>
      <div className={s.toolbar}>
        <span className="hud-label">{rows.length} test instance{rows.length === 1 ? "" : "s"}</span>
        <div className={s.spacer} />
        <Button size="sm" variant="ghost" onClick={() => tests.refetch()}>
          <IconRefresh /> Refresh
        </Button>
        <Button variant="primary" onClick={() => setShowNew(true)}>
          <IconPlus /> New Campaign
        </Button>
      </div>

      <Panel title="Test Instances" flush live>
        {tests.loading && !tests.data ? (
          <div style={{ padding: "var(--space-6)", display: "grid", placeItems: "center" }}>
            <Spinner />
          </div>
        ) : rows.length === 0 ? (
          <EmptyState
            title="No campaigns yet"
            hint="Launch a test to drive SIP traffic at a target."
            action={
              <Button variant="primary" size="sm" onClick={() => setShowNew(true)}>
                New campaign
              </Button>
            }
          />
        ) : (
          <div className={ui.tableWrap}>
            <table className={ui.table}>
              <thead>
                <tr>
                  <th>Test</th>
                  <th>Scenario</th>
                  <th>Target</th>
                  <th className={ui.numCell}>CPS</th>
                  <th className={ui.numCell}>Calls</th>
                  <th className={ui.numCell}>Success</th>
                  <th className={ui.numCell}>Uptime</th>
                  <th>State</th>
                  <th style={{ textAlign: "right" }}>Control</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((t) => {
                  const isRunning = t.state === "running";
                  const scenario = t.scenario_file.replace(/^.*[\\/]/, "").replace(/\.xml$/, "");
                  return (
                    <tr key={t.id}>
                      <td style={{ color: "var(--text-bright)", fontWeight: 600 }}>{t.id}</td>
                      <td style={{ color: "var(--text-muted)" }}>{scenario}</td>
                      <td style={{ color: "var(--text-muted)" }}>
                        {t.remote_host}:{t.remote_port}
                        <span style={{ marginLeft: 6, textTransform: "uppercase", color: "var(--text-faint)" }}>
                          {t.transport}
                        </span>
                      </td>
                      <td className={ui.numCell}>{num(t.stats.calls_per_second, 1)}</td>
                      <td className={ui.numCell}>{int(t.stats.total_calls)}</td>
                      <td
                        className={ui.numCell}
                        style={{ color: t.stats.success_rate >= 95 ? "var(--signal)" : "var(--amber)" }}
                      >
                        {pct(t.stats.success_rate)}
                      </td>
                      <td className={ui.numCell}>{duration(t.stats.uptime_seconds)}</td>
                      <td>
                        <Badge tone={statusTone(t.state)} pulse={isRunning}>
                          {t.state}
                        </Badge>
                      </td>
                      <td>
                        <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
                          {isRunning ? (
                            <>
                              <Button
                                size="sm"
                                variant="ghost"
                                icon
                                title="Adjust rate"
                                onClick={() => {
                                  setRateFor(t.id);
                                  setRate(t.call_rate);
                                }}
                              >
                                <IconSliders />
                              </Button>
                              <Button size="sm" variant="danger" icon title="Stop" onClick={() => stop(t.id)}>
                                <IconStop />
                              </Button>
                            </>
                          ) : (
                            <Button size="sm" variant="ghost" icon title="Remove" onClick={() => remove(t.id)}>
                              <IconTrash />
                            </Button>
                          )}
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

      {/* ---- New campaign modal ---- */}
      <Modal
        open={showNew}
        title={<><IconPlay /> New Campaign</>}
        onClose={() => setShowNew(false)}
        footer={
          <ModalActions onCancel={() => setShowNew(false)} onConfirm={launch} confirmLabel="Launch" />
        }
      >
        <Field label="Campaign name" hint="Leave blank to auto-generate an id.">
          <input
            value={form.name}
            onChange={(e) => set("name", e.target.value)}
            placeholder="edge-soak"
          />
        </Field>
        <Field label="Scenario">
          <select value={form.scenario} onChange={(e) => set("scenario", e.target.value)}>
            {(scenarios.data?.scenarios ?? [{ name: "basic_call" }]).map((sc) => (
              <option key={sc.name} value={sc.name}>
                {sc.name}
              </option>
            ))}
          </select>
        </Field>
        <FieldRow>
          <Field label="Remote host">
            <input
              value={form.remote_host}
              onChange={(e) => set("remote_host", e.target.value)}
              placeholder="10.20.8.40"
            />
          </Field>
          <Field label="Remote port">
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
          <Field label="Call rate (cps)">
            <input
              type="number"
              value={form.call_rate}
              onChange={(e) => set("call_rate", Number(e.target.value))}
            />
          </Field>
          <Field label="Concurrent limit">
            <input
              type="number"
              value={form.call_limit}
              onChange={(e) => set("call_limit", Number(e.target.value))}
            />
          </Field>
          <Field label="Max calls" hint="0 = unlimited">
            <input
              type="number"
              value={form.max_calls}
              onChange={(e) => set("max_calls", Number(e.target.value))}
            />
          </Field>
        </FieldRow>
      </Modal>

      {/* ---- Rate control modal ---- */}
      <Modal
        open={rateFor !== null}
        title={<><IconSliders /> Adjust Call Rate</>}
        onClose={() => setRateFor(null)}
        footer={<ModalActions onCancel={() => setRateFor(null)} onConfirm={applyRate} confirmLabel="Apply" />}
      >
        <Field label={`Target rate · ${rateFor ?? ""}`}>
          <div className={s.rangeRow}>
            <input
              type="range"
              min={0}
              max={500}
              value={rate}
              onChange={(e) => setRate(Number(e.target.value))}
            />
            <input
              type="number"
              style={{ width: 90 }}
              value={rate}
              onChange={(e) => setRate(Number(e.target.value))}
            />
            <span className="hud-label">cps</span>
          </div>
        </Field>
      </Modal>
    </>
  );
}
