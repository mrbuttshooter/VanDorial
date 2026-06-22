import { useState } from "react";
import s from "./pages.module.css";
import { Panel } from "@/components/ui/Panel";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Modal, ModalActions } from "@/components/ui/Modal";
import { Field, EmptyState, Spinner } from "@/components/ui/Misc";
import { IconLayers, IconPlus, IconTrash } from "@/components/icons";
import { useAsync } from "@/hooks/useAsync";
import { api } from "@/lib/api";
import { useToast } from "@/components/ui/Toast";
import type { Scenario } from "@/lib/types";

const STARTER_XML = `<?xml version="1.0" encoding="ISO-8859-1" ?>
<scenario name="Custom Scenario">
  <send retrans="500">
    <![CDATA[
      INVITE sip:[service]@[remote_ip]:[remote_port] SIP/2.0
      Via: SIP/2.0/[transport] [local_ip]:[local_port];branch=[branch]
      From: sipp <sip:sipp@[local_ip]>;tag=[call_number]
      To: sut <sip:[service]@[remote_ip]>
      Call-ID: [call_id]
      CSeq: 1 INVITE
      Content-Length: 0
    ]]>
  </send>
  <recv response="200" rtd="true"/>
  <send><![CDATA[ ACK sip:[service]@[remote_ip] SIP/2.0 ]]></send>
</scenario>`;

export function Scenarios() {
  const list = useAsync(() => api.listScenarios(), []);
  const toast = useToast();
  const [view, setView] = useState<{ name: string; content: string } | null>(null);
  const [showNew, setShowNew] = useState(false);
  const [name, setName] = useState("");
  const [xml, setXml] = useState(STARTER_XML);

  const open = async (sc: Scenario) => {
    try {
      const res = await api.getScenario(sc.name);
      setView(res);
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    }
  };

  const save = async () => {
    if (!name.trim()) {
      toast.error("Scenario name required.");
      return;
    }
    try {
      await api.saveScenario(name.trim(), xml);
      toast.ok(`Saved scenario · ${name}`);
      setShowNew(false);
      setName("");
      setXml(STARTER_XML);
      list.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    }
  };

  const del = async (sc: Scenario) => {
    try {
      await api.deleteScenario(sc.name);
      toast.warn(`Deleted ${sc.name}`);
      list.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    }
  };

  // Show only the loop scenarios for now (demo) — the generic SIP test
  // scenarios (basic_call, options_ping, stress_test, …) aren't used by the
  // loop product, so hide them from the UI to avoid confusion. Remove this
  // filter to show every scenario again.
  const scenarios = (list.data?.scenarios ?? []).filter((sc) =>
    sc.name.toLowerCase().includes("loop"),
  );

  return (
    <>
      <div className={s.toolbar}>
        <span className="hud-label">{scenarios.length} scenarios</span>
        <div className={s.spacer} />
        <Button variant="primary" onClick={() => setShowNew(true)}>
          <IconPlus /> New Scenario
        </Button>
      </div>

      {list.loading && !list.data ? (
        <Panel>
          <div style={{ display: "grid", placeItems: "center", padding: "var(--space-6)" }}>
            <Spinner />
          </div>
        </Panel>
      ) : scenarios.length === 0 ? (
        <Panel>
          <EmptyState title="No scenarios found" hint="Create a custom SIP flow to get started." />
        </Panel>
      ) : (
        <div className={s.cards}>
          {scenarios.map((sc) => (
            <article className={s.card} key={sc.name}>
              <div className={s.cardTop}>
                <span className={s.cardName}>{sc.name}</span>
                <Badge tone={sc.type === "builtin" ? "cyan" : "violet"}>{sc.type}</Badge>
              </div>
              <p className={s.cardDesc}>{sc.description}</p>
              <div className={s.cardActions}>
                <Button size="sm" variant="ghost" onClick={() => open(sc)}>
                  <IconLayers /> View XML
                </Button>
                <div className={s.spacer} />
                {sc.type === "custom" && (
                  <Button size="sm" variant="ghost" icon title="Delete" onClick={() => del(sc)}>
                    <IconTrash />
                  </Button>
                )}
              </div>
            </article>
          ))}
        </div>
      )}

      <Modal
        open={view !== null}
        title={<><IconLayers /> {view?.name}</>}
        onClose={() => setView(null)}
        footer={<Button variant="ghost" onClick={() => setView(null)}>Close</Button>}
      >
        <pre className={s.code}>{view?.content}</pre>
      </Modal>

      <Modal
        open={showNew}
        title={<><IconPlus /> New Scenario</>}
        onClose={() => setShowNew(false)}
        footer={<ModalActions onCancel={() => setShowNew(false)} onConfirm={save} confirmLabel="Save" />}
      >
        <Field label="Name">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="my_custom_flow" />
        </Field>
        <Field label="SIPp XML">
          <textarea
            rows={16}
            value={xml}
            onChange={(e) => setXml(e.target.value)}
            style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-sm)", lineHeight: 1.6, resize: "vertical" }}
          />
        </Field>
      </Modal>
    </>
  );
}
