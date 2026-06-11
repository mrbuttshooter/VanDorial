import { useEffect, useRef, type ReactNode } from "react";
import styles from "./ui.module.css";
import { Button } from "./Button";

interface Props {
  open: boolean;
  title: ReactNode;
  onClose: () => void;
  footer?: ReactNode;
  children: ReactNode;
}

/** Accessible modal: focus trap, Escape to close, scrim click to dismiss. */
export function Modal({ open, title, onClose, footer, children }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  // Keep the latest onClose in a ref so the keydown handler always calls the
  // current one WITHOUT putting onClose in the effect deps. Parents pass a fresh
  // arrow each render; if onClose were a dep the effect would re-run on every
  // keystroke and yank focus back to the dialog — losing the field you're typing in.
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCloseRef.current();
      if (e.key === "Tab") trapFocus(e, ref.current);
    };
    document.addEventListener("keydown", onKey);
    // Move focus to the first field in the BODY (not the header's close X, which
    // would otherwise win as the first <button> in DOM order). Runs ONCE per open.
    const first = bodyRef.current?.querySelector<HTMLElement>(
      "input, select, textarea, button",
    );
    first?.focus();
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  if (!open) return null;

  return (
    <div className={styles.scrim} onMouseDown={onClose}>
      <div
        className={styles.modal}
        role="dialog"
        aria-modal="true"
        ref={ref}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <header className={styles.modalHead}>
          <div className={styles.panelTitle}>{title}</div>
          <button className={styles.iconBtn} onClick={onClose} aria-label="Close">
            <XIcon />
          </button>
        </header>
        <div className={styles.modalBody} ref={bodyRef}>{children}</div>
        {footer && <footer className={styles.modalFoot}>{footer}</footer>}
      </div>
    </div>
  );
}

export function ModalActions({
  onCancel,
  confirmLabel = "Confirm",
  onConfirm,
  danger = false,
  disabled = false,
}: {
  onCancel: () => void;
  confirmLabel?: string;
  onConfirm: () => void;
  danger?: boolean;
  disabled?: boolean;
}) {
  return (
    <>
      <Button variant="ghost" onClick={onCancel}>
        Cancel
      </Button>
      <Button variant={danger ? "danger" : "primary"} onClick={onConfirm} disabled={disabled}>
        {confirmLabel}
      </Button>
    </>
  );
}

function trapFocus(e: KeyboardEvent, root: HTMLElement | null) {
  if (!root) return;
  const nodes = root.querySelectorAll<HTMLElement>(
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
  );
  if (nodes.length === 0) return;
  const first = nodes[0];
  const last = nodes[nodes.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}

function XIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}
