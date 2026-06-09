import { useEffect, useRef } from "react";
import { hexA } from "./chartUtils";

interface Props {
  data: number[];
  color?: string;
  fill?: boolean;
}

/** Tiny phosphor sparkline on a DPR-correct canvas. */
export function Sparkline({ data, color = "var(--signal)", fill = true }: Props) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const w = rect.width;
    const h = rect.height;
    canvas.width = Math.max(1, Math.floor(w * dpr));
    canvas.height = Math.max(1, Math.floor(h * dpr));
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);

    if (data.length < 2) return;
    const resolved = resolveColor(ctx, color);

    const min = Math.min(...data);
    const max = Math.max(...data);
    const span = max - min || 1;
    const pad = 2;
    const x = (i: number) => (i / (data.length - 1)) * w;
    const y = (v: number) => h - pad - ((v - min) / span) * (h - pad * 2);

    if (fill) {
      const grad = ctx.createLinearGradient(0, 0, 0, h);
      grad.addColorStop(0, hexA(resolved, 0.28));
      grad.addColorStop(1, hexA(resolved, 0));
      ctx.beginPath();
      ctx.moveTo(0, h);
      data.forEach((v, i) => ctx.lineTo(x(i), y(v)));
      ctx.lineTo(w, h);
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();
    }

    ctx.beginPath();
    data.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
    ctx.strokeStyle = resolved;
    ctx.lineWidth = 1.5;
    ctx.lineJoin = "round";
    ctx.shadowColor = resolved;
    ctx.shadowBlur = 6;
    ctx.stroke();

    // Leading dot
    ctx.shadowBlur = 8;
    ctx.beginPath();
    ctx.arc(x(data.length - 1), y(data[data.length - 1]), 2, 0, Math.PI * 2);
    ctx.fillStyle = resolved;
    ctx.fill();
  }, [data, color, fill]);

  return <canvas ref={ref} style={{ width: "100%", height: "100%", display: "block" }} />;
}

/** Resolve a CSS var / named color to a concrete rgb(a) string via the canvas. */
function resolveColor(ctx: CanvasRenderingContext2D, color: string): string {
  if (color.startsWith("var(")) {
    const name = color.slice(4, -1).trim();
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    if (v) return v;
  }
  ctx.fillStyle = color;
  return ctx.fillStyle;
}
