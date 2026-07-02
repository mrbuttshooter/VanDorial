import { num } from "@/lib/format";

/* 24-bar diurnal CPS sparkline (inline, no chart lib) — shared by the
   Calculator result and the preset form's profile preview. Bars are scaled to
   the series peak; an all-zero series renders flat.

   Named CpsSparkline to avoid confusion with the shared
   components/charts/Sparkline (a different, generic chart). */
export function CpsSparkline({ cps }: { cps: number[] }) {
  const peak = cps.reduce((m, v) => (v > m ? v : m), 0);
  return (
    <div style={{ display: "flex", alignItems: "flex-end", gap: 2, height: 60 }}>
      {cps.map((v, h) => (
        <div
          key={h}
          title={`${h}:00 — ${num(v)} cps`}
          style={{
            flex: 1,
            height: `${peak > 0 ? (v / peak) * 100 : 0}%`,
            minHeight: 1,
            background: "var(--signal, #4ade80)",
            borderRadius: 1,
          }}
        />
      ))}
    </div>
  );
}
