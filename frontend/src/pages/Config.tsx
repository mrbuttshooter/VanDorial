import { useState } from "react";
import s from "./pages.module.css";
import { Panel } from "@/components/ui/Panel";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Spinner, Field } from "@/components/ui/Misc";
import { useToast } from "@/components/ui/Toast";
import { useAsync } from "@/hooks/useAsync";
import { api, getApiKey, setApiKey } from "@/lib/api";
import { MOCK_ENABLED } from "@/lib/mock";

export function Config() {
  const health = useAsync(() => api.health(), [], 5000);
  const toast = useToast();

  const [savedKey, setSavedKey] = useState<string | null>(getApiKey());
  const [keyInput, setKeyInput] = useState<string>(getApiKey() ?? "");

  const saveKey = () => {
    const trimmed = keyInput.trim();
    setApiKey(trimmed || null);
    setSavedKey(trimmed || null);
    toast.ok(trimmed ? "API key saved" : "API key cleared");
  };

  const clearKey = () => {
    setApiKey(null);
    setSavedKey(null);
    setKeyInput("");
    toast.info("API key cleared");
  };

  return (
    <div style={{ display: "grid", gap: "var(--space-5)" }}>
      <div className={s.split}>
        <Panel title="Server">
          {health.loading && !health.data ? (
            <div style={{ display: "grid", placeItems: "center", padding: "var(--space-5)" }}>
              <Spinner />
            </div>
          ) : (
            <dl className={s.kv}>
              <dt>Service</dt>
              <dd>{health.data?.name ?? "GenCall"}</dd>
              <dt>Version</dt>
              <dd>{health.data?.version ?? "—"}</dd>
              <dt>Status</dt>
              <dd>
                <Badge tone={health.data?.status === "ok" ? "signal" : "crit"} pulse>
                  {health.data?.status ?? "unknown"}
                </Badge>
              </dd>
              <dt>Active tests</dt>
              <dd>{health.data?.active_tests ?? 0}</dd>
              <dt>Data source</dt>
              <dd>
                <Badge tone={MOCK_ENABLED ? "amber" : "cyan"}>
                  {MOCK_ENABLED ? "mock simulator" : "live backend"}
                </Badge>
              </dd>
            </dl>
          )}
        </Panel>

        <Panel title="Runtime">
          <dl className={s.kv}>
            <dt>Console build</dt>
            <dd>2.1.0</dd>
            <dt>Stream transport</dt>
            <dd>WebSocket /ws</dd>
            <dt>API base</dt>
            <dd>/api</dd>
            <dt>Stats interval</dt>
            <dd>1s</dd>
          </dl>
          <p style={{ marginTop: "var(--space-4)", fontSize: "var(--fs-sm)", color: "var(--text-muted)", lineHeight: 1.6 }}>
            Server-side configuration lives in <code>gencall/etc/gencall.cfg</code>. SIP timers, RTP
            port range, database engine and SIPp binary path are set there and applied on restart.
          </p>
        </Panel>
      </div>

      <Panel title="Authentication">
        <dl className={s.kv} style={{ marginBottom: "var(--space-4)" }}>
          <dt>API key</dt>
          <dd>
            <Badge tone={savedKey ? "signal" : "amber"}>
              {savedKey ? "configured" : "not set"}
            </Badge>
          </dd>
        </dl>
        <Field
          label="X-API-Key"
          hint={
            MOCK_ENABLED
              ? "Only used against a live backend — the mock simulator ignores it."
              : "The backend enforces this header on every endpoint except /api/health. Stored locally in this browser."
          }
        >
          <input
            type="password"
            value={keyInput}
            onChange={(e) => setKeyInput(e.target.value)}
            placeholder="gc_…"
            autoComplete="off"
            spellCheck={false}
          />
        </Field>
        <div style={{ display: "flex", gap: "var(--space-3)", marginTop: "var(--space-3)" }}>
          <Button variant="primary" onClick={saveKey}>
            Save key
          </Button>
          <Button variant="ghost" onClick={clearKey} disabled={!savedKey && !keyInput}>
            Clear
          </Button>
        </div>
      </Panel>
    </div>
  );
}
