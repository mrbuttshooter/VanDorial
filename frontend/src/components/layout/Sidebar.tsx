import { NavLink } from "react-router-dom";
import styles from "./layout.module.css";
import {
  IconGauge,
  IconWave,
  IconLayers,
  IconPlug,
  IconLoop,
  IconTerminal,
  IconHistory,
  IconSettings,
  IconBolt,
} from "../icons";

interface NavItem {
  to: string;
  label: string;
  icon: typeof IconGauge;
}

const GROUPS: { label: string; items: NavItem[] }[] = [
  {
    label: "Fleet",
    items: [
      { to: "/fleet", label: "Fleet Overview", icon: IconWave },
      { to: "/nodes", label: "Nodes", icon: IconPlug },
      { to: "/groups", label: "Groups", icon: IconLayers },
    ],
  },
  {
    label: "Operations",
    items: [
      { to: "/", label: "Dashboard", icon: IconGauge },
      { to: "/campaigns", label: "Campaigns", icon: IconBolt },
      { to: "/loops", label: "Loops", icon: IconLoop },
      { to: "/servers", label: "Servers", icon: IconPlug },
      { to: "/scenarios", label: "Scenarios", icon: IconLayers },
      { to: "/connectors", label: "Connectors", icon: IconPlug },
    ],
  },
  {
    label: "Telemetry",
    items: [
      { to: "/console", label: "Console", icon: IconTerminal },
      { to: "/performance", label: "Performance", icon: IconWave },
      { to: "/history", label: "History", icon: IconHistory },
    ],
  },
  {
    label: "System",
    items: [{ to: "/config", label: "Configuration", icon: IconSettings }],
  },
];

export function Sidebar({ activeTests }: { activeTests: number }) {
  return (
    <aside className={styles.sidebar}>
      <div className={styles.brand}>
        <div className={styles.brandMark}>
          <IconWave width={18} height={18} />
        </div>
        <div className={styles.brandText}>
          <span className={styles.brandName}>GENCALL</span>
          <span className={styles.brandSub}>NOC Console</span>
        </div>
      </div>

      <nav>
        {GROUPS.map((group) => (
          <div className={styles.navGroup} key={group.label}>
            <div className={styles.navGroupLabel}>{group.label}</div>
            {group.items.map(({ to, label, icon: Icon }) => (
              <NavLink
                key={to}
                to={to}
                end={to === "/"}
                className={({ isActive }) =>
                  `${styles.navLink} ${isActive ? styles.navLinkActive : ""}`
                }
              >
                <Icon />
                <span>{label}</span>
                {to === "/" && activeTests > 0 && (
                  <span className={styles.navCount}>{activeTests}</span>
                )}
              </NavLink>
            ))}
          </div>
        ))}
      </nav>

      <div className={styles.sidebarFoot}>
        <span>GenCall v2.0 · SIP traffic generator</span>
        <span>build · console 2.1.0</span>
      </div>
    </aside>
  );
}
