import styles from "./ui.module.css";
import { COLORS, type Tone } from "./tone";

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
