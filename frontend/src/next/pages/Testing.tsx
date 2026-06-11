import { Link } from "react-router-dom";
import { Panel } from "@/components/ui/Panel";
import { Button } from "@/components/ui/Button";
import { IconBolt, IconLayers, IconPlug } from "@/components/icons";

/**
 * Testing: the generic, ad-hoc SIP tooling kept OUT of the loop business but
 * still one click away — one-shot reachability tests, saved scenarios, and SIP
 * connectors. These open the classic console pages (unchanged).
 */
const TOOLS = [
  { to: "/campaigns", icon: IconBolt, title: "One-shot tests", desc: "Ad-hoc reachability / call tests (basic_call, register, auth) against a target." },
  { to: "/scenarios", icon: IconLayers, title: "Scenarios", desc: "Saved SIP message flows (the XML scenarios SIPp runs)." },
  { to: "/connectors", icon: IconPlug, title: "Connectors", desc: "Named SIP endpoints / trunks you test against." },
];

export function Testing() {
  return (
    <>
      <div style={{ marginBottom: "var(--space-4)" }}>
        <h1 style={{ margin: 0, color: "var(--text-bright)" }}>Testing</h1>
        <p style={{ margin: "4px 0 0", color: "var(--text-muted)", fontSize: "var(--fs-sm)" }}>
          Generic SIP tooling, separate from the loop business.
        </p>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(220px,1fr))", gap: "var(--space-3)" }}>
        {TOOLS.map((t) => (
          <Panel key={t.to} title={<span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}><t.icon width={17} height={17} /> {t.title}</span>}>
            <p style={{ margin: "0 0 var(--space-3)", color: "var(--text-muted)", fontSize: "var(--fs-sm)" }}>{t.desc}</p>
            <Link to={t.to}><Button size="sm" variant="ghost">Open ↗</Button></Link>
          </Panel>
        ))}
      </div>
    </>
  );
}
