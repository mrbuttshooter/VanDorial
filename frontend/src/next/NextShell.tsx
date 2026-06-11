import { NavLink, Outlet } from "react-router-dom";
import {
  IconGauge,
  IconWave,
  IconLoop,
  IconBolt,
  IconSettings,
  IconPlug,
} from "@/components/icons";

/**
 * NEXT (v3) console shell — a parallel, loop-first UI mounted at /next so the
 * existing console stays untouched. Five business screens + a Testing area for
 * the generic one-shot SIP tooling. Reuses the dark "ember" theme + ui kit.
 */
const NAV = [
  { to: "/next", end: true, label: "Overview", icon: IconGauge },
  { to: "/next/servers", label: "Servers", icon: IconPlug },
  { to: "/next/routes", label: "Routes", icon: IconLoop },
  { to: "/next/activity", label: "Activity", icon: IconWave },
  { to: "/next/testing", label: "Testing", icon: IconBolt },
  { to: "/next/settings", label: "Settings", icon: IconSettings },
];

export function NextShell() {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "208px 1fr", height: "100vh" }}>
      <aside
        style={{
          background: "var(--bg-base)",
          borderRight: "1px solid var(--line)",
          display: "flex",
          flexDirection: "column",
          padding: "var(--space-3)",
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "var(--space-3)",
            padding: "var(--space-3) var(--space-2)",
            marginBottom: "var(--space-3)",
          }}
        >
          <IconLoop width={20} height={20} />
          <span style={{ fontWeight: 600, color: "var(--text-bright)" }}>VanDorial</span>
          <span
            style={{
              marginLeft: "auto",
              fontSize: "var(--fs-2xs)",
              color: "var(--signal)",
              border: "1px solid var(--signal-dim)",
              borderRadius: "var(--radius-sm, 4px)",
              padding: "1px 6px",
            }}
          >
            v3
          </span>
        </div>
        <nav style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          {NAV.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.end}
              style={({ isActive }) => ({
                display: "flex",
                alignItems: "center",
                gap: "var(--space-3)",
                padding: "var(--space-2) var(--space-3)",
                borderRadius: "var(--radius-sm, 6px)",
                fontSize: "var(--fs-sm)",
                color: isActive ? "var(--text-bright)" : "var(--text-muted)",
                background: isActive ? "var(--bg-panel)" : "transparent",
                border: isActive ? "1px solid var(--line)" : "1px solid transparent",
                textDecoration: "none",
              })}
            >
              <n.icon width={17} height={17} />
              {n.label}
            </NavLink>
          ))}
        </nav>
        <a
          href="#/"
          style={{
            marginTop: "auto",
            fontSize: "var(--fs-2xs)",
            color: "var(--text-faint)",
            textDecoration: "none",
            padding: "var(--space-2)",
          }}
        >
          ← classic console
        </a>
      </aside>
      <main style={{ overflowY: "auto", padding: "var(--space-5) var(--space-6)" }}>
        <Outlet />
      </main>
    </div>
  );
}
