import s from "../pages.module.css";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { statusTone } from "@/components/ui/tone";
import { IconStop, IconWave } from "@/components/icons";
import { duration, int, num, pct } from "@/lib/format";
import type { LoopCampaign, LoopStats } from "@/lib/types";
import { acd, asr, minutes, ner, targetProgress } from "./loopsUtils";

/* Per-campaign live card (running loops). Extracted verbatim from Loops.tsx. */
export function LoopCard({
  campaign,
  stats,
  onStop,
  onCapture,
}: {
  campaign: LoopCampaign;
  stats: LoopStats | undefined;
  onStop: () => void;
  onCapture: () => void;
}) {
  const isRunning = campaign.status === "running";
  const st = stats;
  const progress = targetProgress(campaign, st);

  const failuresOut = st?.failures?.out ?? {};
  const failureRows = Object.entries(failuresOut).sort((a, b) => b[1] - a[1]);

  return (
    <div className={s.card}>
      <div className={s.cardTop}>
        <div>
          <div className={s.cardName}>{campaign.name}</div>
          <div style={{ fontSize: "var(--fs-xs)", color: "var(--text-faint)" }}>
            {campaign.local_ip ? (
              <span title="Source IP (node)">{campaign.local_ip} → </span>
            ) : null}
            {campaign.dest_host}:{campaign.dest_port}
            <span style={{ marginLeft: 6, textTransform: "uppercase" }}>
              {campaign.transport}
            </span>
            {campaign.box && campaign.box !== "local" ? (
              <span style={{ marginLeft: 6, color: "var(--cyan)" }} title="remote worker">
                ⇄ {campaign.box.replace(/^https?:\/\//, "")}
              </span>
            ) : null}
          </div>
        </div>
        <Badge tone={statusTone(campaign.status)} pulse={isRunning}>
          {campaign.status}
        </Badge>
      </div>

      {/* ASR / NER / ACD */}
      <div className={s.tiles} style={{ gridTemplateColumns: "1fr 1fr 1fr", gap: "var(--space-3)" }}>
        <div className={s.mini}>
          <span
            className={s.miniVal}
            style={{ color: st && asr(st) >= 50 ? "var(--signal)" : "var(--amber)" }}
          >
            {st ? pct(asr(st)) : "—"}
          </span>
          <span className={s.miniLabel}>ASR · answered</span>
        </div>
        <div className={s.mini}>
          <span
            className={s.miniVal}
            style={{
              color: st
                ? ner(st) >= 99 ? "var(--signal)" : ner(st) >= 90 ? "var(--amber)" : "var(--crit)"
                : undefined,
            }}
          >
            {st ? pct(ner(st)) : "—"}
          </span>
          <span className={s.miniLabel}>NER · routed</span>
        </div>
        <div className={s.mini}>
          <span className={s.miniVal}>{st ? duration(acd(st)) : "—"}</span>
          <span className={s.miniLabel}>ACD · avg duration</span>
        </div>
      </div>

      {/* Minutes out vs in */}
      <dl className={s.kv}>
        <dt>Minutes OUT</dt>
        <dd>{num(minutes(st?.minutes_out_ms))}</dd>
        <dt>Minutes IN</dt>
        <dd>{num(minutes(st?.minutes_in_ms))}</dd>
        <dt>Calls out / answered</dt>
        <dd>
          {int(st?.calls_out ?? 0)} / {int(st?.answered_out ?? 0)}
        </dd>
        <dt>Δ avg (in − out)</dt>
        <dd>{st ? `${num(st.delta_avg_ms / 1000)} s` : "—"}</dd>
      </dl>

      {/* Loop completion % (matched inbound ÷ answered outbound) */}
      <div>
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            fontSize: "var(--fs-2xs)",
            textTransform: "uppercase",
            letterSpacing: "var(--tracking-wide)",
            color: "var(--text-faint)",
            marginBottom: 4,
          }}
        >
          <span>Loop completion</span>
          <span style={{ color: "var(--text-bright)" }}>
            {st ? pct(st.completion_pct) : "—"}
          </span>
        </div>
        <Meter value={st?.completion_pct ?? 0} tone="cyan" />
        {progress != null && (
          <>
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                fontSize: "var(--fs-2xs)",
                textTransform: "uppercase",
                letterSpacing: "var(--tracking-wide)",
                color: "var(--text-faint)",
                margin: "8px 0 4px",
              }}
            >
              <span>
                Target ·{" "}
                {campaign.target_calls > 0
                  ? `${int(campaign.target_calls)} calls`
                  : `${int(campaign.target_minutes)} min`}
              </span>
              <span style={{ color: "var(--text-bright)" }}>{pct(progress)}</span>
            </div>
            <Meter value={progress} tone="signal" />
          </>
        )}
      </div>

      {/* Failures by SIP code */}
      <div>
        <div
          style={{
            fontSize: "var(--fs-2xs)",
            textTransform: "uppercase",
            letterSpacing: "var(--tracking-wide)",
            color: "var(--text-faint)",
            marginBottom: 6,
          }}
        >
          Failures by SIP code (out)
        </div>
        {failureRows.length === 0 ? (
          <div style={{ fontSize: "var(--fs-xs)", color: "var(--text-muted)" }}>
            No outbound failures.
          </div>
        ) : (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            {failureRows.map(([code, count]) => (
              <span
                key={code}
                style={{
                  display: "inline-flex",
                  gap: 6,
                  alignItems: "baseline",
                  padding: "2px 8px",
                  borderRadius: "var(--r-sm)",
                  border: "1px solid var(--line)",
                  background: "var(--bg-inset)",
                  fontFamily: "var(--font-mono)",
                  fontSize: "var(--fs-xs)",
                }}
              >
                <span style={{ color: "var(--crit)" }}>{code}</span>
                <span style={{ color: "var(--text-bright)" }}>{int(count)}</span>
              </span>
            ))}
          </div>
        )}
      </div>

      <div className={s.cardActions}>
        <div style={{ flex: 1 }} />
        {isRunning && (
          <Button size="sm" variant="ghost" onClick={onCapture} title="Capture a pcap trace of this loop">
            <IconWave /> Capture
          </Button>
        )}
        {isRunning && (
          <Button size="sm" variant="danger" onClick={onStop}>
            <IconStop /> Stop
          </Button>
        )}
      </div>
    </div>
  );
}

/* Slim progress meter (0–100) shared by completion + target bars. */
export function Meter({ value, tone }: { value: number; tone: "cyan" | "signal" }) {
  const color = tone === "cyan" ? "var(--cyan)" : "var(--signal)";
  return (
    <div
      style={{
        height: 6,
        borderRadius: 3,
        background: "var(--bg-inset)",
        border: "1px solid var(--line)",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          height: "100%",
          width: `${Math.max(0, Math.min(100, value))}%`,
          background: color,
          boxShadow: `0 0 8px ${color}`,
          transition: "width 0.4s var(--ease)",
        }}
      />
    </div>
  );
}
