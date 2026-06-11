import { useState } from "react";
import { Link } from "react-router-dom";
import { Panel } from "@/components/ui/Panel";
import { Button } from "@/components/ui/Button";
import { Field } from "@/components/ui/Misc";
import { useToast } from "@/components/ui/Toast";
import { getApiKey, setApiKey } from "@/lib/api";

/**
 * Settings: the API key the console uses, plus read-only reminders of the
 * fleet/safety knobs (which live in gencall.cfg on each box).
 */
export function Settings() {
  const toast = useToast();
  const [key, setKey] = useState(getApiKey() ?? "");

  const save = () => {
    setApiKey(key.trim() || null);
    toast.ok("API key saved");
  };

  return (
    <>
      <div style={{ marginBottom: "var(--space-4)" }}>
        <h1 style={{ margin: 0, color: "var(--text-bright)" }}>Settings</h1>
        <p style={{ margin: "4px 0 0", color: "var(--text-muted)", fontSize: "var(--fs-sm)" }}>
          Console access + a map of the fleet/safety knobs.
        </p>
      </div>

      <div style={{ maxWidth: 460, display: "grid", gap: "var(--space-4)" }}>
        <Panel title="Console access">
          <Field label="API key" hint="Sent as X-API-Key on every request. Stored in this browser only.">
            <input value={key} onChange={(e) => setKey(e.target.value)} placeholder="gc_…" />
          </Field>
          <Button variant="primary" size="sm" onClick={save}>Save key</Button>
        </Panel>

        <Panel title="Fleet & safety (set in gencall.cfg per box)">
          <dl style={{ margin: 0, fontSize: "var(--fs-sm)", display: "grid", gridTemplateColumns: "auto 1fr", gap: "6px 12px" }}>
            <dt style={{ color: "var(--text-muted)" }}>Fleet token</dt>
            <dd style={{ margin: 0, color: "var(--text-bright)" }}>[fleet] token — same on every box</dd>
            <dt style={{ color: "var(--text-muted)" }}>Auto-discovery</dt>
            <dd style={{ margin: 0, color: "var(--text-bright)" }}>[fleet] announce / discovery</dd>
            <dt style={{ color: "var(--text-muted)" }}>Headless worker</dt>
            <dd style={{ margin: 0, color: "var(--text-bright)" }}>[web] serve_console = false</dd>
            <dt style={{ color: "var(--text-muted)" }}>Dest allow-list</dt>
            <dd style={{ margin: 0, color: "var(--text-bright)" }}>[loops] dest_allowlist (MADA)</dd>
            <dt style={{ color: "var(--text-muted)" }}>Trust whitelist</dt>
            <dd style={{ margin: 0, color: "var(--text-bright)" }}>[trust] whitelist → MADA IPs</dd>
          </dl>
          <p style={{ marginTop: "var(--space-3)", marginBottom: 0, fontSize: "var(--fs-xs)", color: "var(--text-faint)" }}>
            Full configuration still lives on the classic <Link to="/config">Config</Link> page.
          </p>
        </Panel>
      </div>
    </>
  );
}
