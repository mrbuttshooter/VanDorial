import { useMemo, useState } from "react";
import s from "./pages.module.css";
import { Panel } from "@/components/ui/Panel";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { statusTone } from "@/components/ui/tone";
import { Modal, ModalActions } from "@/components/ui/Modal";
import { Field, FieldRow, EmptyState, Spinner } from "@/components/ui/Misc";
import {
  IconPlay,
  IconStop,
  IconPlus,
  IconRefresh,
  IconDownload,
} from "@/components/icons";
import { useAsync } from "@/hooks/useAsync";
import { useStream } from "@/hooks/useStream";
import { api } from "@/lib/api";
import { useToast } from "@/components/ui/Toast";
import { duration, int, num, pct } from "@/lib/format";
import type {
  GenerateNumbersResult,
  LoopCampaign,
  LoopStats,
  StartLoopRequest,
  Transport,
} from "@/lib/types";

/** Number-generation picker state (Country → Sale Zone cascade). */
interface GenState {
  originCountry: string;
  originZone: string;
  destCountry: string;
  destZone: string;
  count: number;
  length: number;
}

const BLANK_GEN: GenState = {
  originCountry: "",
  originZone: "",
  destCountry: "",
  destZone: "",
  count: 500000,
  length: 11,
};

const BLANK: StartLoopRequest = {
  name: "",
  csv_path: "",
  dest_host: "",
  dest_port: 5060,
  transport: "udp",
  rate: 1,
  max_concurrent: 10,
  duration_mode: "fixed",
  duration_s: 180,
  duration_max_s: 0,
  match_key: "exact",
  target_calls: 0,
  target_minutes: 0,
};

/** ms → minutes, rounded to 1 decimal. */
function minutes(ms: number | null | undefined): number {
  if (ms == null || Number.isNaN(ms)) return 0;
  return ms / 60000;
}

/** ASR: answered ÷ originated, as a 0–100 percentage. */
function asr(st: LoopStats): number {
  return st.calls_out > 0 ? (st.answered_out / st.calls_out) * 100 : 0;
}

/** ACD: average answered-call duration in seconds (minutes_out ÷ answered). */
function acd(st: LoopStats): number {
  return st.answered_out > 0 ? st.minutes_out_ms / st.answered_out / 1000 : 0;
}

/** Loop completion as a fraction toward a calls/minutes target (0–100), or
 *  null when the campaign runs until stopped (no target). */
function targetProgress(c: LoopCampaign, st: LoopStats | undefined): number | null {
  if (c.target_calls && c.target_calls > 0) {
    const done = st?.calls_out ?? 0;
    return Math.min(100, (done / c.target_calls) * 100);
  }
  if (c.target_minutes && c.target_minutes > 0) {
    const done = minutes(st?.minutes_out_ms);
    return Math.min(100, (done / c.target_minutes) * 100);
  }
  return null;
}

export function Loops() {
  const loops = useAsync(() => api.listLoops(), [], 3000);
  const toast = useToast();

  // Latest loop_stats snapshot per campaign, fed live by the WS 'loops' topic
  // (backend loop_matcher shape) and keyed by campaign_id.
  const [stats, setStats] = useState<Record<string, LoopStats>>({});
  useStream<LoopStats>("loops", (st) => {
    if (!st || !st.campaign_id) return;
    setStats((prev) => ({ ...prev, [st.campaign_id]: st }));
  });

  const [showNew, setShowNew] = useState(false);
  const [form, setForm] = useState<StartLoopRequest>(BLANK);
  const [gen, setGen] = useState<GenState>(BLANK_GEN);
  const [busy, setBusy] = useState(false);

  // Servers (source IPs) and the sale-zone tree power the loop form pickers.
  const servers = useAsync(() => api.listServers(), []);
  const zoneTree = useAsync(() => api.saleZones(), []);

  const countries = useMemo(
    () => (zoneTree.data?.countries ?? []),
    [zoneTree.data],
  );
  const zonesFor = (countryName: string): string[] =>
    countries.find((c) => c.name === countryName)?.zones ?? [];

  const set = <K extends keyof StartLoopRequest>(k: K, v: StartLoopRequest[K]) =>
    setForm((f) => ({ ...f, [k]: v }));
  const setG = <K extends keyof GenState>(k: K, v: GenState[K]) =>
    setGen((g) => ({ ...g, [k]: v }));

  const resetForm = () => {
    setForm(BLANK);
    setGen(BLANK_GEN);
  };

  const launch = async () => {
    if (!form.dest_host?.trim()) {
      toast.error("Destination (MADA) host is required.");
      return;
    }
    // Numbers come from EITHER the Country→Zone pickers (generated now) or a
    // manually-entered server-side CSV path. Pickers win when both zones are set.
    const usingPickers = gen.originZone && gen.destZone;
    if (!usingPickers && !form.csv_path?.trim()) {
      toast.error("Pick an origin + drop zone, or enter a CSV path.");
      return;
    }
    setBusy(true);
    try {
      let csvPath = form.csv_path ?? "";
      if (usingPickers) {
        const r: GenerateNumbersResult = await api.generateNumbers({
          origin_zone: gen.originZone,
          dest_zone: gen.destZone,
          count: gen.count,
          length: gen.length,
        });
        csvPath = r.csv_path;
        toast.ok(`Generated ${r.count.toLocaleString()} numbers`);
      }
      const res = await api.startLoop({ ...form, csv_path: csvPath });
      toast.ok(`Loop campaign launched · ${res.campaign.id}`);
      setShowNew(false);
      resetForm();
      loops.refetch();
    } catch (e) {
      toast.error(`Launch failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  };

  const stop = async (id: string) => {
    try {
      await api.stopLoop(id);
      toast.warn(`Stopped ${id}`);
      loops.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    }
  };

  const download = async (id: string) => {
    try {
      await api.downloadLoopRecordsCsv(id);
    } catch (e) {
      toast.error(`Download failed: ${e instanceof Error ? e.message : e}`);
    }
  };

  const campaigns = loops.data?.campaigns ?? [];
  // Running campaigns first, then most-recently created.
  const ordered = useMemo(() => {
    return [...campaigns].sort((a, b) => {
      if (a.status === "running" && b.status !== "running") return -1;
      if (b.status === "running" && a.status !== "running") return 1;
      return (b.created_at ?? "").localeCompare(a.created_at ?? "");
    });
  }, [campaigns]);

  const runningCount = campaigns.filter((c) => c.status === "running").length;

  return (
    <>
      <div className={s.toolbar}>
        <span className="hud-label">
          {runningCount} running · {campaigns.length} total
        </span>
        <div className={s.spacer} />
        <Button size="sm" variant="ghost" onClick={() => loops.refetch()}>
          <IconRefresh /> Refresh
        </Button>
        <Button variant="primary" onClick={() => setShowNew(true)}>
          <IconPlus /> New Loop Campaign
        </Button>
      </div>

      {loops.loading && !loops.data ? (
        <Panel title="Loop Campaigns" flush live>
          <div style={{ padding: "var(--space-6)", display: "grid", placeItems: "center" }}>
            <Spinner />
          </div>
        </Panel>
      ) : ordered.length === 0 ? (
        <Panel title="Loop Campaigns" flush>
          <EmptyState
            title="No loop campaigns yet"
            hint="Start a campaign to drive minutes-for-minutes loop traffic at a destination."
            action={
              <Button variant="primary" size="sm" onClick={() => setShowNew(true)}>
                New loop campaign
              </Button>
            }
          />
        </Panel>
      ) : (
        <div className={s.cards}>
          {ordered.map((c) => (
            <LoopCard
              key={c.id}
              campaign={c}
              stats={stats[c.id]}
              onStop={() => stop(c.id)}
              onDownload={() => download(c.id)}
            />
          ))}
        </div>
      )}

      {/* ---- New campaign modal ---- */}
      <Modal
        open={showNew}
        title={<><IconPlay /> New Loop Campaign</>}
        onClose={() => { setShowNew(false); resetForm(); }}
        footer={
          <ModalActions
            onCancel={() => { setShowNew(false); resetForm(); }}
            onConfirm={launch}
            confirmLabel="Launch"
            disabled={busy}
          />
        }
      >
        <FieldRow>
          <Field label="Campaign name" hint="Blank = auto id.">
            <input
              value={form.name}
              onChange={(e) => set("name", e.target.value)}
              placeholder="ng-lagos-to-guinea"
            />
          </Field>
          <Field
            label="Source server (IP)"
            hint={
              servers.data?.servers?.length
                ? "One loop per IP."
                : "Add servers on the Servers page."
            }
          >
            <select
              value={form.local_ip ?? ""}
              onChange={(e) => set("local_ip", e.target.value)}
            >
              <option value="">OS default route</option>
              {(servers.data?.servers ?? [])
                .filter((sv) => sv.enabled)
                .map((sv) => (
                  <option key={sv.id} value={sv.ip}>
                    {sv.name} — {sv.ip}
                  </option>
                ))}
            </select>
          </Field>
        </FieldRow>

        {/* ---- Numbers: Country → Sale Zone cascade ---- */}
        <div className={s.formSection}>Numbers (drop zones)</div>
        <FieldRow>
          <Field label="Origin country">
            <select
              value={gen.originCountry}
              onChange={(e) =>
                setGen((g) => ({ ...g, originCountry: e.target.value, originZone: "" }))
              }
            >
              <option value="">
                {zoneTree.loading ? "Loading…" : "Select country"}
              </option>
              {countries.map((c) => (
                <option key={c.name} value={c.name}>{c.name}</option>
              ))}
            </select>
          </Field>
          <Field label="Origin sale zone (A / oad)">
            <select
              value={gen.originZone}
              disabled={!gen.originCountry}
              onChange={(e) => setG("originZone", e.target.value)}
            >
              <option value="">Select zone</option>
              {zonesFor(gen.originCountry).map((z) => (
                <option key={z} value={z}>{z}</option>
              ))}
            </select>
          </Field>
        </FieldRow>
        <FieldRow>
          <Field label="Drop country">
            <select
              value={gen.destCountry}
              onChange={(e) =>
                setGen((g) => ({ ...g, destCountry: e.target.value, destZone: "" }))
              }
            >
              <option value="">
                {zoneTree.loading ? "Loading…" : "Select country"}
              </option>
              {countries.map((c) => (
                <option key={c.name} value={c.name}>{c.name}</option>
              ))}
            </select>
          </Field>
          <Field label="Drop sale zone (B / dad)">
            <select
              value={gen.destZone}
              disabled={!gen.destCountry}
              onChange={(e) => setG("destZone", e.target.value)}
            >
              <option value="">Select zone</option>
              {zonesFor(gen.destCountry).map((z) => (
                <option key={z} value={z}>{z}</option>
              ))}
            </select>
          </Field>
          <Field label="How many" hint="Random draw pool.">
            <input
              type="number"
              value={gen.count}
              onChange={(e) => setG("count", Number(e.target.value))}
            />
          </Field>
        </FieldRow>
        <details>
          <summary className={s.advancedSummary}>Advanced: use a server-side CSV path instead</summary>
          <Field label="Number-pair CSV path" hint="Overrides the zone pickers. A;B per row.">
            <input
              value={form.csv_path}
              onChange={(e) => set("csv_path", e.target.value)}
              placeholder="/tmp/loop_numbers.csv"
            />
          </Field>
        </details>

        {/* ---- Destination (MADA switch) ---- */}
        <div className={s.formSection}>Destination (MADA switch)</div>
        <FieldRow>
          <Field label="Destination host">
            <input
              value={form.dest_host}
              onChange={(e) => set("dest_host", e.target.value)}
              placeholder="10.20.8.40"
            />
          </Field>
          <Field label="Destination port">
            <input
              type="number"
              value={form.dest_port}
              onChange={(e) => set("dest_port", Number(e.target.value))}
            />
          </Field>
          <Field label="Transport">
            <select
              value={form.transport}
              onChange={(e) => set("transport", e.target.value as Transport)}
            >
              <option value="udp">UDP</option>
              <option value="tcp">TCP</option>
              <option value="tls">TLS</option>
            </select>
          </Field>
        </FieldRow>
        <FieldRow>
          <Field label="Call rate (cps)">
            <input
              type="number"
              step="0.1"
              value={form.rate}
              onChange={(e) => set("rate", Number(e.target.value))}
            />
          </Field>
          <Field label="Max concurrent">
            <input
              type="number"
              value={form.max_concurrent}
              onChange={(e) => set("max_concurrent", Number(e.target.value))}
            />
          </Field>
          <Field label="Match key" hint="exact or suffixN">
            <input
              value={form.match_key}
              onChange={(e) => set("match_key", e.target.value)}
              placeholder="exact"
            />
          </Field>
        </FieldRow>
        <FieldRow>
          <Field label="Duration mode">
            <select
              value={form.duration_mode}
              onChange={(e) =>
                set("duration_mode", e.target.value as StartLoopRequest["duration_mode"])
              }
            >
              <option value="fixed">Fixed</option>
              <option value="range">Range</option>
            </select>
          </Field>
          <Field label={form.duration_mode === "range" ? "Min duration (s)" : "Duration (s)"}>
            <input
              type="number"
              value={form.duration_s}
              onChange={(e) => set("duration_s", Number(e.target.value))}
            />
          </Field>
          {form.duration_mode === "range" && (
            <Field label="Max duration (s)">
              <input
                type="number"
                value={form.duration_max_s}
                onChange={(e) => set("duration_max_s", Number(e.target.value))}
              />
            </Field>
          )}
        </FieldRow>
        <FieldRow>
          <Field label="Target calls" hint="0 = until stopped">
            <input
              type="number"
              value={form.target_calls}
              onChange={(e) => set("target_calls", Number(e.target.value))}
            />
          </Field>
          <Field label="Target minutes" hint="0 = until stopped">
            <input
              type="number"
              value={form.target_minutes}
              onChange={(e) => set("target_minutes", Number(e.target.value))}
            />
          </Field>
        </FieldRow>
      </Modal>
    </>
  );
}

/* ---- Per-campaign live card ---------------------------------------------- */

function LoopCard({
  campaign,
  stats,
  onStop,
  onDownload,
}: {
  campaign: LoopCampaign;
  stats: LoopStats | undefined;
  onStop: () => void;
  onDownload: () => void;
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
            {campaign.dest_host}:{campaign.dest_port}
            <span style={{ marginLeft: 6, textTransform: "uppercase" }}>
              {campaign.transport}
            </span>
          </div>
        </div>
        <Badge tone={statusTone(campaign.status)} pulse={isRunning}>
          {campaign.status}
        </Badge>
      </div>

      {/* ASR / ACD */}
      <div className={s.tiles} style={{ gridTemplateColumns: "1fr 1fr", gap: "var(--space-3)" }}>
        <div className={s.mini}>
          <span
            className={s.miniVal}
            style={{ color: st && asr(st) >= 50 ? "var(--signal)" : "var(--amber)" }}
          >
            {st ? pct(asr(st)) : "—"}
          </span>
          <span className={s.miniLabel}>ASR · success</span>
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
        <Button size="sm" variant="ghost" onClick={onDownload}>
          <IconDownload /> Download CSV
        </Button>
        <div style={{ flex: 1 }} />
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
function Meter({ value, tone }: { value: number; tone: "cyan" | "signal" }) {
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
