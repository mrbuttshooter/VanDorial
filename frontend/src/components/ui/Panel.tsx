import type { ReactNode } from "react";
import styles from "./ui.module.css";

interface Props {
  title?: ReactNode;
  actions?: ReactNode;
  live?: boolean;
  flush?: boolean;
  className?: string;
  bodyClassName?: string;
  children: ReactNode;
}

/** A bordered instrument panel with an optional HUD header. */
export function Panel({
  title,
  actions,
  live = false,
  flush = false,
  className = "",
  bodyClassName = "",
  children,
}: Props) {
  return (
    <section className={`${styles.panel} ${live ? styles.panelLive : ""} ${className}`}>
      {(title || actions) && (
        <header className={styles.panelHead}>
          {title && <div className={styles.panelTitle}>{title}</div>}
          {actions && <div>{actions}</div>}
        </header>
      )}
      <div className={`${flush ? styles.panelBodyFlush : styles.panelBody} ${bodyClassName}`}>
        {children}
      </div>
    </section>
  );
}
