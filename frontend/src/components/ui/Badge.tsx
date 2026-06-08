import styles from "./ui.module.css";

type Tone = "signal" | "amber" | "crit" | "cyan" | "muted" | "violet";

const COLORS: Record<Tone, string> = {
  signal: "var(--signal)",
  amber: "var(--amber)",
  crit: "var(--crit)",
  cyan: "var(--cyan)",
  muted: "var(--text-muted)",
  violet: "var(--violet)",
};

/** Maps domain statuses to a tone so colors stay consistent everywhere. */
export function statusTone(status: string): Tone {
  switch (status) {
    case "running":
      return "signal";
    case "pending":
    case "starting":
    case "stopping":
      return "amber";
    case "failed":
      return "crit";
    case "completed":
      return "cyan";
    case "stopped":
    case "idle":
    default:
      return "muted";
  }
}

export function Badge({
  children,
  tone = "muted",
  pulse = false,
}: {
  children: React.ReactNode;
  tone?: Tone;
  pulse?: boolean;
}) {
  return (
    <span className={styles.badge} style={{ color: COLORS[tone] }}>
      <span className={`${styles.dot} ${pulse ? styles.dotPulse : ""}`} />
      {children}
    </span>
  );
}

export function StatusDot({ tone = "muted", pulse = false }: { tone?: Tone; pulse?: boolean }) {
  return (
    <span
      className={`${styles.dot} ${pulse ? styles.dotPulse : ""}`}
      style={{ color: COLORS[tone] }}
    />
  );
}
