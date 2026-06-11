import { useEffect, useRef, useState } from "react";
import { hexA } from "./chartUtils";
import styles from "./charts.module.css";

export interface Series {
  label: string;
  color: string; // CSS var or hex
  values: number[];
  axis?: "left" | "right";
}

interface Props {
  series: Series[];
  height?: number;
  /** Format a value for the axis / hover readout. */
  format?: (v: number) => string;
  formatRight?: (v: number) => string;
}

/**
 * Multi-series phosphor line chart on a DPR-correct canvas. Draws a graph-paper
 * grid, glowing traces, a sweep cursor on hover, and a live value readout.
 * Hand-rolled (no chart lib) for full control of the NOC aesthetic + perf.
 */
export function TimeSeriesChart({ series, height = 240, format = String, formatRight }: Props) {
  const ref = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<number | null>(null);

  useEffect(() => {
    const canvas = ref.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const dpr = window.devicePixelRatio || 1;
    const w = wrap.clientWidth;
    const h = height;
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    canvas.style.width = `${w}px`;
    canvas.style.height = `${h}px`;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const padL = 44;
    const padR = formatRight ? 44 : 14;
    const padT = 12;
    const padB = 20;
    const plotW = w - padL - padR;
    const plotH = h - padT - padB;

    const css = (c: string) => resolve(ctx, c);
    const grid = css("var(--grid-line)");
    const lineCol = css("var(--line)");
    const textCol = css("var(--text-faint)");

    const left = series.filter((s) => (s.axis ?? "left") === "left");
    const right = series.filter((s) => s.axis === "right");
    const range = (arr: Series[]) => {
      const all = arr.flatMap((s) => s.values);
      if (!all.length) return [0, 1] as const;
      const mx = Math.max(...all);
      return [0, mx <= 0 ? 1 : mx * 1.15] as const;
    };
    const [lMin, lMax] = range(left);
    const [rMin, rMax] = range(right);
    const len = Math.max(0, ...series.map((s) => s.values.length));

    const X = (i: number) => padL + (len <= 1 ? 0 : (i / (len - 1)) * plotW);
    const yFor = (v: number, min: number, max: number) =>
      padT + plotH - ((v - min) / (max - min || 1)) * plotH;

    // ---- grid ----
    ctx.lineWidth = 1;
    ctx.strokeStyle = grid;
    ctx.fillStyle = textCol;
    ctx.font = '10px "IBM Plex Mono", monospace';
    const rows = 4;
    for (let r = 0; r <= rows; r++) {
      const y = padT + (r / rows) * plotH;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(w - padR, y);
      ctx.strokeStyle = r === rows ? lineCol : grid;
      ctx.stroke();
      const lv = lMax - (r / rows) * (lMax - lMin);
      ctx.textAlign = "right";
      ctx.fillText(format(lv), padL - 6, y + 3);
      if (formatRight) {
        const rv = rMax - (r / rows) * (rMax - rMin);
        ctx.textAlign = "left";
        ctx.fillText(formatRight(rv), w - padR + 6, y + 3);
      }
    }

    // ---- traces ----
    const drawSeries = (s: Series, min: number, max: number) => {
      if (s.values.length < 2) return;
      const col = css(s.color);
      const grad = ctx.createLinearGradient(0, padT, 0, padT + plotH);
      grad.addColorStop(0, hexA(col, 0.22));
      grad.addColorStop(1, hexA(col, 0));
      ctx.beginPath();
      ctx.moveTo(X(0), padT + plotH);
      s.values.forEach((v, i) => ctx.lineTo(X(i), yFor(v, min, max)));
      ctx.lineTo(X(s.values.length - 1), padT + plotH);
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();

      ctx.beginPath();
      s.values.forEach((v, i) =>
        i ? ctx.lineTo(X(i), yFor(v, min, max)) : ctx.moveTo(X(i), yFor(v, min, max)),
      );
      ctx.strokeStyle = col;
      ctx.lineWidth = 1.75;
      ctx.lineJoin = "round";
      ctx.shadowColor = col;
      ctx.shadowBlur = 7;
      ctx.stroke();
      ctx.shadowBlur = 0;
    };
    left.forEach((s) => drawSeries(s, lMin, lMax));
    right.forEach((s) => drawSeries(s, rMin, rMax));

    // ---- hover sweep + points ----
    if (hover != null && len > 1) {
      const i = Math.max(0, Math.min(len - 1, hover));
      const hx = X(i);
      ctx.strokeStyle = hexA(css("var(--signal)"), 0.4);
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(hx, padT);
      ctx.lineTo(hx, padT + plotH);
      ctx.stroke();
      ctx.setLineDash([]);
      series.forEach((s) => {
        const v = s.values[i];
        if (v == null) return;
        const min = (s.axis ?? "left") === "left" ? lMin : rMin;
        const max = (s.axis ?? "left") === "left" ? lMax : rMax;
        const col = css(s.color);
        ctx.beginPath();
        ctx.arc(hx, yFor(v, min, max), 3, 0, Math.PI * 2);
        ctx.fillStyle = col;
        ctx.shadowColor = col;
        ctx.shadowBlur = 8;
        ctx.fill();
        ctx.shadowBlur = 0;
      });
    }
  }, [series, height, hover, format, formatRight]);

  const idx = hover != null && series[0] ? Math.min(series[0].values.length - 1, hover) : null;

  return (
    <div className={styles.chart} ref={wrapRef}>
      <canvas
        ref={ref}
        onMouseMove={(e) => {
          const wrap = wrapRef.current;
          if (!wrap) return;
          const rect = wrap.getBoundingClientRect();
          const padL = 44;
          const padR = formatRight ? 44 : 14;
          const plotW = rect.width - padL - padR;
          const len = Math.max(0, ...series.map((s) => s.values.length));
          const rel = (e.clientX - rect.left - padL) / (plotW || 1);
          setHover(Math.round(rel * (len - 1)));
        }}
        onMouseLeave={() => setHover(null)}
      />
      <div className={styles.legend}>
        {series.map((s) => (
          <span key={s.label} className={styles.legendItem}>
            <i style={{ background: s.color, boxShadow: `0 0 8px ${s.color}` }} />
            {s.label}
            {idx != null && s.values[idx] != null && (
              <b className="mono-num">
                {(s.axis === "right" ? formatRight ?? format : format)(s.values[idx])}
              </b>
            )}
          </span>
        ))}
      </div>
    </div>
  );
}

function resolve(ctx: CanvasRenderingContext2D, color: string): string {
  if (color.startsWith("var(")) {
    const name = color.slice(4, -1).trim();
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    if (v) return v;
  }
  ctx.fillStyle = color;
  return ctx.fillStyle;
}
