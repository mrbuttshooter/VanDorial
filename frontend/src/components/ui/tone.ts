/* Tone vocabulary + status→tone mapping, shared by Badge/StatusDot and the
   pages. Kept out of the component file so React Fast Refresh treats Badge.tsx
   as a component-only module. */

export type Tone = "signal" | "amber" | "crit" | "cyan" | "muted" | "violet";

export const COLORS: Record<Tone, string> = {
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
