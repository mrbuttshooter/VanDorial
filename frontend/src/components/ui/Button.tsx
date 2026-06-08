import type { ButtonHTMLAttributes, ReactNode } from "react";
import styles from "./ui.module.css";

type Variant = "default" | "primary" | "danger" | "ghost";

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: "sm" | "md";
  icon?: boolean;
  children?: ReactNode;
}

export function Button({
  variant = "default",
  size = "md",
  icon = false,
  className = "",
  children,
  ...rest
}: Props) {
  const cls = [
    styles.btn,
    variant === "primary" && styles.btn_primary,
    variant === "danger" && styles.btn_danger,
    variant === "ghost" && styles.btn_ghost,
    size === "sm" && styles.btn_sm,
    icon && styles.btn_icon,
    className,
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <button className={cls} {...rest}>
      {children}
    </button>
  );
}
