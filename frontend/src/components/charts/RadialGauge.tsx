import { useEffect, useRef } from "react";

interface Props {
  /** 0–100. */
  value: number;
  size?: number;
  label?: string;
  /** Thresholds for color: >=good → signal, >=warn → amber, else crit. */
  good?: number;
  warn?: number;
}

/** A 270° dial — success-rate / health readout with a phosphor arc. */
export function RadialGauge({ value, size = 132, label, good = 95, warn = 85 }: Props) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = size * dpr;
    canvas.height = size * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, size, size);

    const cx = size / 2;
    const cy = size / 2;
    const r = size / 2 - 12;
    const start = Math.PI * 0.75;
    const end = Math.PI * 2.25;
    const v = Math.max(0, Math.min(100, value));
    const tip = start + (v / 100) * (end - start);

    const css = (name: string) =>
      getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    const color = v >= good ? css("--ok") : v >= warn ? css("--amber") : css("--crit");

    // track
    ctx.beginPath();
    ctx.arc(cx, cy, r, start, end);
    ctx.strokeStyle = css("--line-strong");
    ctx.lineWidth = 8;
    ctx.lineCap = "round";
    ctx.stroke();

    // value arc
    ctx.beginPath();
    ctx.arc(cx, cy, r, start, tip);
    ctx.strokeStyle = color;
    ctx.lineWidth = 8;
    ctx.lineCap = "round";
    ctx.shadowColor = color;
    ctx.shadowBlur = 12;
    ctx.stroke();
    ctx.shadowBlur = 0;

    // readout
    ctx.fillStyle = css("--text-bright");
    ctx.textAlign = "center";
    ctx.font = '700 24px "Archivo", sans-serif';
    ctx.fillText(v.toFixed(1), cx, cy + 8);
    ctx.fillStyle = css("--text-muted");
    ctx.font = '10px "IBM Plex Mono", monospace';
    ctx.fillText("%", cx, cy + 24);
    if (label) {
      ctx.fillStyle = css("--text-faint");
      ctx.font = '9px "IBM Plex Mono", monospace';
      ctx.fillText(label.toUpperCase(), cx, cy - 26);
    }
  }, [value, size, label, good, warn]);

  return <canvas ref={ref} style={{ width: size, height: size }} />;
}
