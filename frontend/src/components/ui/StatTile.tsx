import type { ReactNode } from "react";
import styles from "./stattile.module.css";
import { Sparkline } from "../charts/Sparkline";

type Tone = "signal" | "amber" | "crit" | "cyan";

interface Props {
  label: string;
  value: ReactNode;
  unit?: string;
  tone?: Tone;
  spark?: number[];
  sub?: ReactNode;
  live?: boolean;
}

const TONE_VAR: Record<Tone, string> = {
  signal: "var(--signal)",
  amber: "var(--amber)",
  crit: "var(--crit)",
  cyan: "var(--cyan)",
};

/** A primary readout: HUD label, large glowing value, optional sparkline. */
export function StatTile({ label, value, unit, tone = "signal", spark, sub, live }: Props) {
  const color = TONE_VAR[tone];
  return (
    <div className={styles.tile} style={{ ["--tile-accent" as string]: color }}>
      <div className={styles.head}>
        <span className={styles.label}>{label}</span>
        {live && <span className={styles.liveDot} />}
      </div>
      <div className={styles.valueRow}>
        <span className={`${styles.value} mono-num`}>{value}</span>
        {unit && <span className={styles.unit}>{unit}</span>}
      </div>
      {spark && spark.length > 1 ? (
        <div className={styles.spark}>
          <Sparkline data={spark} color={color} />
        </div>
      ) : (
        sub && <div className={styles.sub}>{sub}</div>
      )}
    </div>
  );
}
