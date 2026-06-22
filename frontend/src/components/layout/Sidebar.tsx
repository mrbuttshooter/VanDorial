import { useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
import styles from "./layout.module.css";
import {
  IconGauge,
  IconWave,
  IconLayers,
  IconPlug,
  IconLoop,
  IconHistory,
  IconSettings,
  IconPower,
} from "../icons";
import { api, requireAuth } from "@/lib/api";
import { useToast } from "../ui/Toast";

interface NavItem {
  to: string;
  label: string;
  icon: typeof IconGauge;
}

const GROUPS: { label: string; items: NavItem[] }[] = [
  {
    label: "Fleet",
    items: [
      { to: "/fleet", label: "Fleet", icon: IconWave },
      { to: "/nodes", label: "Nodes", icon: IconPlug },
      { to: "/groups", label: "Groups", icon: IconLayers },
    ],
  },
  {
    label: "Operations",
    items: [
      { to: "/", label: "Dashboard", icon: IconGauge },
      // Hidden from the nav for now (demo) — routes still exist, just not shown.
      // { to: "/campaigns", label: "Campaigns", icon: IconBolt },
      { to: "/loops", label: "Loops", icon: IconLoop },
      { to: "/scenarios", label: "Scenarios", icon: IconLayers },
      // { to: "/connectors", label: "Connectors", icon: IconPlug },
    ],
  },
  {
    label: "Telemetry",
    items: [
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
  const toast = useToast();
  const [username, setUsername] = useState<string | null>(null);
  const [loggingOut, setLoggingOut] = useState(false);

  useEffect(() => {
    let alive = true;
    api
      .me()
      .then((me) => alive && setUsername(me.username))
      .catch(() => {
        /* token may be gone; the auth gate handles the bounce to login */
      });
    return () => {
      alive = false;
    };
  }, []);

  const logout = async () => {
    setLoggingOut(true);
    try {
      await api.logout();
      toast.info("Signed out.");
    } catch {
      /* best effort — clear locally regardless */
    } finally {
      requireAuth();
    }
  };

  return (
    <aside className={styles.sidebar}>
      <div className={styles.brand}>
        <div className={styles.brandMark}>
          <IconWave width={18} height={18} />
        </div>
        <div className={styles.brandText}>
          <span className={styles.brandName}>GENCALL</span>
          <span className={styles.brandSub}>SMC Console</span>
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
        <div className={styles.sidebarUser}>
          <span className={styles.sidebarUserName}>
            {username ? `Signed in · ${username}` : "Signed in"}
          </span>
          <button
            type="button"
            className={styles.logout}
            onClick={logout}
            disabled={loggingOut}
            title="Sign out"
          >
            <IconPower width={14} height={14} />
            <span>Logout</span>
          </button>
        </div>
        <span>GenCall v2.0 · SIP traffic generator</span>
        <span>build · console 2.1.0</span>
        <span>Done by Sergio &amp; Joey</span>
      </div>
    </aside>
  );
}
