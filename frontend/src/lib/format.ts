/* Small, pure formatters for instrument readouts. Tested in format.test.ts. */

/** Compact integer with thousands separators: 12840 → "12,840". */
export function int(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  return Math.round(n).toLocaleString("en-US");
}

/** Fixed-decimal number, tabular: 3.14159 → "3.14". */
export function num(n: number | null | undefined, digits = 2): string {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

/** Percentage from a 0–100 value: 98.4 → "98.4%". */
export function pct(n: number | null | undefined, digits = 1): string {
  if (n == null || Number.isNaN(n)) return "—";
  return `${num(n, digits)}%`;
}

/** Abbreviated large numbers: 1500 → "1.5K", 2_300_000 → "2.3M". */
export function abbrev(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  const abs = Math.abs(n);
  if (abs >= 1e9) return `${num(n / 1e9, 1)}B`;
  if (abs >= 1e6) return `${num(n / 1e6, 1)}M`;
  if (abs >= 1e3) return `${num(n / 1e3, 1)}K`;
  return String(Math.round(n));
}

/** Seconds → "1h 02m 09s" / "02m 09s" / "9s". */
export function duration(seconds: number | null | undefined): string {
  if (seconds == null || Number.isNaN(seconds) || seconds < 0) return "—";
  const s = Math.floor(seconds % 60);
  const m = Math.floor((seconds / 60) % 60);
  const h = Math.floor(seconds / 3600);
  const pad = (v: number) => String(v).padStart(2, "0");
  if (h > 0) return `${h}h ${pad(m)}m ${pad(s)}s`;
  if (m > 0) return `${pad(m)}m ${pad(s)}s`;
  return `${s}s`;
}

/** Milliseconds with unit: 42.5 → "42.5 ms". */
export function ms(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  return `${num(n, 1)} ms`;
}

/** Epoch seconds → "14:09:42". */
export function clock(tsSeconds: number): string {
  const d = new Date(tsSeconds * 1000);
  return d.toLocaleTimeString("en-GB", { hour12: false });
}

/** ISO string → "2026-06-08 14:09". Returns "—" for null. */
export function datetime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const pad = (v: number) => String(v).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(
    d.getHours(),
  )}:${pad(d.getMinutes())}`;
}

/** "relative ago" from an ISO timestamp: "3m ago", "just now". */
export function ago(iso: string | null | undefined, nowMs = Date.now()): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const sec = Math.max(0, (nowMs - t) / 1000);
  if (sec < 10) return "just now";
  if (sec < 60) return `${Math.floor(sec)}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}
