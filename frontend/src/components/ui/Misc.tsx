import type { ReactNode } from "react";
import styles from "./ui.module.css";

export function EmptyState({
  mark = "//",
  title,
  hint,
  action,
}: {
  mark?: string;
  title: string;
  hint?: string;
  action?: ReactNode;
}) {
  return (
    <div className={styles.empty}>
      <div className={styles.emptyMark}>{mark}</div>
      <div style={{ color: "var(--text)", fontWeight: 600 }}>{title}</div>
      {hint && <div style={{ fontSize: "var(--fs-sm)" }}>{hint}</div>}
      {action && <div style={{ marginTop: "var(--space-3)" }}>{action}</div>}
    </div>
  );
}

export function Spinner() {
  return <span className={styles.spinner} aria-label="Loading" role="status" />;
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <div className={styles.field}>
      <label>{label}</label>
      {children}
      {hint && <span className={styles.hint}>{hint}</span>}
    </div>
  );
}

export function FieldRow({ children }: { children: ReactNode }) {
  return <div className={styles.fieldRow}>{children}</div>;
}
