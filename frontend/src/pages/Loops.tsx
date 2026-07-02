import { Fragment, useEffect, useMemo, useState } from "react";
import s from "./pages.module.css";
import ui from "@/components/ui/ui.module.css";
import { Panel } from "@/components/ui/Panel";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Modal, ModalActions } from "@/components/ui/Modal";
import { Field, FieldRow, EmptyState, Spinner } from "@/components/ui/Misc";
import {
  IconPlay,
  IconPlus,
  IconRefresh,
  IconTrash,
  IconWave,
} from "@/components/icons";
import { useAsync } from "@/hooks/useAsync";
import { useStream } from "@/hooks/useStream";
import { api } from "@/lib/api";
import { useToast } from "@/components/ui/Toast";
import { duration, int, num } from "@/lib/format";
import type {
  LoopCampaign,
  LoopPreset,
  LoopPresetRequest,
  LoopStats,
  TrafficCalcResult,
  Transport,
} from "@/lib/types";
// Extracted into ./loops/* (surgical split; no behavior change).
import { PRESET_BLANK, freshest } from "./loops/loopsUtils";
import { CpsSparkline } from "./loops/CpsSparkline";
import { LoopCard } from "./loops/LoopCard";
import { RunModal } from "./loops/RunModal";
import { CaptureModal } from "./loops/CaptureModal";

export function Loops() {
  const loops = useAsync(() => api.listLoopsFleet(), [], 3000);
  const presets = useAsync(() => api.listLoopPresets(), []);
  const toast = useToast();

  // Latest loop_stats snapshot per campaign, fed live by the WS 'loops' topic.
  const [stats, setStats] = useState<Record<string, LoopStats>>({});
  useStream<LoopStats>("loops", (st) => {
    if (!st || !st.campaign_id) return;
    setStats((prev) => ({ ...prev, [st.campaign_id]: st }));
  });

  // Preset create/edit modal.
  const [showPreset, setShowPreset] = useState(false);
  const [editId, setEditId] = useState<number | null>(null);
  const [form, setForm] = useState<LoopPresetRequest>(PRESET_BLANK);
  const [busy, setBusy] = useState(false);

  // Live diurnal preview for the preset form's "Traffic profile" section: the
  // 24-bar CPS curve sized from the form's target_minutes + the ACD (duration_s)
  // + the knobs. Recomputed (debounced) whenever a profile input changes while
  // the section is enabled.
  const [profilePreview, setProfilePreview] = useState<TrafficCalcResult | null>(null);

  // Traffic Calculator modal: size peak/avg CPS + concurrency from a daily
  // minutes target + ACD + a diurnal curve (no engine — pure sizing).
  const [showCalc, setShowCalc] = useState(false);
  const [calc, setCalc] = useState({ target_minutes: 1000000, acd_s: 120, night_floor: 0.25 });
  const [calcRes, setCalcRes] = useState<TrafficCalcResult | null>(null);
  const [calcBusy, setCalcBusy] = useState(false);

  const runCalc = async () => {
    setCalcBusy(true);
    try {
      const res = await api.trafficCalc({
        target_minutes: Number(calc.target_minutes), acd_s: Number(calc.acd_s),
        profile: { night_floor: Number(calc.night_floor) },
      });
      setCalcRes(res);
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    } finally {
      setCalcBusy(false);
    }
  };

  // "Apply to new preset": pre-fill a fresh preset with the sized rate +
  // concurrency AND enable the diurnal profile (the knobs + target the rate was
  // sized from), so a run of this preset shapes itself along the same curve.
  const applyCalcToPreset = () => {
    if (!calcRes) return;
    setEditId(null);
    setForm({
      ...PRESET_BLANK,
      rate: calcRes.peak_cps,
      max_concurrent: calcRes.peak_concurrent,
      duration_s: Number(calc.acd_s),
      target_minutes: Number(calc.target_minutes),
      profile_enabled: true,
      night_floor: Number(calc.night_floor),
    });
    setShowCalc(false);
    setShowPreset(true);
  };

  // Run modal (pick node or group for a chosen preset).
  const [runFor, setRunFor] = useState<LoopPreset | null>(null);

  // Trace-capture modal (per running loop): start/stop tcpdump + its captures.
  const [captureFor, setCaptureFor] = useState<LoopCampaign | null>(null);

  // Which preset rows are expanded (to reveal the IPs/loops running from them).
  const [open, setOpen] = useState<Set<number>>(new Set());
  const toggle = (id: number) =>
    setOpen((o) => {
      const n = new Set(o);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  const set = <K extends keyof LoopPresetRequest>(k: K, v: LoopPresetRequest[K]) =>
    setForm((f) => ({ ...f, [k]: v }));

  // Recompute the profile preview when the modal is open, the profile is on, and
  // any sizing input changes. Debounced + abortable so dragging a number input
  // doesn't spam the API or land a stale result.
  useEffect(() => {
    if (!showPreset || !form.profile_enabled) {
      setProfilePreview(null);
      return;
    }
    let cancelled = false;
    const t = setTimeout(() => {
      api
        .trafficCalc({
          target_minutes: Number(form.target_minutes) || 0,
          acd_s: Number(form.duration_s) || 1,
          profile: {
            night_floor: Number(form.night_floor),
            ramp_up_start: Number(form.ramp_up_start),
            plateau_start: Number(form.plateau_start),
            plateau_end: Number(form.plateau_end),
            ramp_down_end: Number(form.ramp_down_end),
            tz_offset: Number(form.tz_offset),
          },
        })
        .then((res) => {
          if (!cancelled) setProfilePreview(res);
        })
        .catch(() => {
          if (!cancelled) setProfilePreview(null);
        });
    }, 300);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [
    showPreset,
    form.profile_enabled,
    form.target_minutes,
    form.duration_s,
    form.night_floor,
    form.ramp_up_start,
    form.plateau_start,
    form.plateau_end,
    form.ramp_down_end,
    form.tz_offset,
  ]);

  const openNew = () => {
    setEditId(null);
    setForm(PRESET_BLANK);
    setShowPreset(true);
  };

  const openEdit = (p: LoopPreset) => {
    setEditId(p.id);
    setForm({
      name: p.name,
      description: p.description,
      dest_host: p.dest_host,
      dest_port: p.dest_port,
      transport: p.transport as Transport,
      rate: p.rate,
      max_concurrent: p.max_concurrent,
      duration_mode: p.duration_mode,
      duration_s: p.duration_s,
      duration_max_s: p.duration_max_s,
      match_key: p.match_key,
      target_calls: p.target_calls,
      target_minutes: p.target_minutes,
      rtp: p.rtp,
      rtp_loop: p.rtp_loop,
      profile_enabled: p.profile_enabled ?? false,
      profile_preset: p.profile_preset ?? "diurnal",
      night_floor: p.night_floor ?? 0.25,
      ramp_up_start: p.ramp_up_start ?? 6,
      plateau_start: p.plateau_start ?? 9,
      plateau_end: p.plateau_end ?? 18,
      ramp_down_end: p.ramp_down_end ?? 22,
      tz_offset: p.tz_offset ?? 0,
    });
    setShowPreset(true);
  };

  const savePreset = async () => {
    if (!form.name?.trim()) {
      toast.error("Preset name is required.");
      return;
    }
    if (!form.dest_host?.trim()) {
      toast.error("Destination (MADA) host is required.");
      return;
    }
    setBusy(true);
    try {
      if (editId != null) {
        await api.updateLoopPreset(editId, form);
        toast.ok("Preset updated");
      } else {
        await api.createLoopPreset(form);
        toast.ok("Preset saved");
      }
      setShowPreset(false);
      presets.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  };

  const delPreset = async (p: LoopPreset) => {
    try {
      await api.deleteLoopPreset(p.id);
      toast.warn(`Deleted preset ${p.name}`);
      presets.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    }
  };

  const stop = async (c: LoopCampaign) => {
    try {
      await api.stopLoopFleet(c.id, c.box ?? "local");
      toast.warn(`Stopped ${c.id}`);
      loops.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    }
  };

  const campaigns = loops.data?.campaigns ?? [];
  const running = useMemo(
    () =>
      campaigns
        .filter((c) => c.status === "running")
        .sort((a, b) => (b.started_at ?? "").localeCompare(a.started_at ?? "")),
    [campaigns],
  );
  const presetRows = presets.data?.presets ?? [];
  // A running campaign is named "<preset>" or "<preset>-<node-ip>". A preset
  // "matches" when the name equals it or starts with "<preset>-". When preset
  // names share a prefix (e.g. "Guinea" and "Guinea-22460"), a campaign matches
  // BOTH — so attribute each campaign to the LONGEST (most specific) matching
  // preset only, otherwise "Guinea-22460-<ip>" wrongly shows under "Guinea" too.
  const matchesPreset = (name: string, presetName: string) =>
    name === presetName || name.startsWith(`${presetName}-`);
  const bestPresetName = (name: string) =>
    presetRows
      .filter((q) => matchesPreset(name, q.name))
      .reduce((best, q) => (q.name.length > best.length ? q.name : best), "");
  const runsForPreset = (p: LoopPreset) =>
    running.filter((c) => bestPresetName(c.name) === p.name);
  const orphanRunning = useMemo(
    () =>
      running.filter(
        (c) => !presetRows.some((p) => c.name === p.name || c.name.startsWith(`${p.name}-`)),
      ),
    [running, presetRows],
  );

  return (
    <>
      <div className={s.toolbar}>
        <span className="hud-label">
          {running.length} running · {presetRows.length} presets
        </span>
        <div className={s.spacer} />
        <Button size="sm" variant="ghost" onClick={() => { loops.refetch(); presets.refetch(); }}>
          <IconRefresh /> Refresh
        </Button>
        <Button variant="ghost" onClick={() => setShowCalc(true)}>
          <IconWave /> Calculator
        </Button>
        <Button variant="primary" onClick={openNew}>
          <IconPlus /> New Preset
        </Button>
      </div>

      {/* ---- Saved loops (presets) — expand a row to see the IPs running from it ---- */}
      <Panel title="Loops (presets)" flush>
        {presets.loading && !presets.data ? (
          <div style={{ padding: "var(--space-6)", display: "grid", placeItems: "center" }}>
            <Spinner />
          </div>
        ) : presetRows.length === 0 ? (
          <EmptyState
            title="No saved loops yet"
            hint="Save a loop recipe (destination + ACD + rate) once, then Run it on a node or group — expand a row to see which IPs are running it."
            action={
              <Button variant="primary" size="sm" onClick={openNew}>
                New preset
              </Button>
            }
          />
        ) : (
          <div className={ui.tableWrap}>
            <table className={ui.table}>
              <thead>
                <tr>
                  <th style={{ width: 28 }}></th>
                  <th>Loop</th>
                  <th>Destination</th>
                  <th>ACD</th>
                  <th className={ui.numCell}>Rate</th>
                  <th>Target</th>
                  <th>Running</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {presetRows.map((p) => {
                  const runs = runsForPreset(p);
                  const isOpen = open.has(p.id);
                  const target =
                    p.target_calls > 0
                      ? `${int(p.target_calls)} calls`
                      : p.target_minutes > 0
                        ? `${int(p.target_minutes)} min`
                        : "until stopped";
                  return (
                    <Fragment key={p.id}>
                      <tr onClick={() => toggle(p.id)} style={{ cursor: "pointer" }}>
                        <td style={{ color: "var(--text-faint)", textAlign: "center" }}>
                          {isOpen ? "▾" : "▸"}
                        </td>
                        <td style={{ color: "var(--text-bright)", fontWeight: 600 }}>
                          {p.name}
                          {p.rtp && (
                            <span
                              title={p.rtp_loop
                                ? "Streams RTP media (PCMA), looped for the whole call"
                                : "Streams RTP media (PCMA), played once per call"}
                              style={{
                                marginLeft: 8, fontSize: "0.7em", fontWeight: 600,
                                color: "var(--cyan)", border: "1px solid var(--cyan)",
                                borderRadius: 3, padding: "0 4px", verticalAlign: "middle",
                              }}
                            >
                              {p.rtp_loop ? "RTP∞" : "RTP"}
                            </span>
                          )}
                        </td>
                        <td style={{ color: "var(--text-muted)" }}>
                          {p.dest_host}:{p.dest_port}
                          <span style={{ marginLeft: 6, textTransform: "uppercase" }}>{p.transport}</span>
                        </td>
                        <td style={{ color: "var(--text-muted)" }}>{duration(p.duration_s)}</td>
                        <td className={ui.numCell}>{p.rate} cps</td>
                        <td style={{ color: "var(--text-muted)" }}>{target}</td>
                        <td>
                          {runs.length > 0 ? (
                            <Badge tone="signal" pulse>{runs.length} running</Badge>
                          ) : (
                            <span style={{ color: "var(--text-faint)" }}>idle</span>
                          )}
                        </td>
                        <td style={{ textAlign: "right", whiteSpace: "nowrap" }} onClick={(e) => e.stopPropagation()}>
                          <Button size="sm" variant="primary" onClick={() => setRunFor(p)}>
                            <IconPlay /> Run
                          </Button>
                          <Button size="sm" variant="ghost" onClick={() => openEdit(p)}>Edit</Button>
                          <Button size="sm" variant="ghost" icon title="Delete preset" onClick={() => delPreset(p)}>
                            <IconTrash />
                          </Button>
                        </td>
                      </tr>
                      {isOpen && (
                        <tr>
                          <td colSpan={8} style={{ background: "var(--bg-inset)", padding: "var(--space-3)" }}>
                            {runs.length === 0 ? (
                              <div style={{ fontSize: "var(--fs-xs)", color: "var(--text-muted)" }}>
                                No loops running from this preset — hit <strong>Run</strong> to start one on a node or group.
                              </div>
                            ) : (
                              <div className={s.cards}>
                                {runs.map((c) => (
                                  <LoopCard
                                    key={c.id}
                                    campaign={c}
                                    stats={freshest(stats[c.id], c.loop_stats ?? undefined)}
                                    onStop={() => stop(c)}
                                    onCapture={() => setCaptureFor(c)}
                                  />
                                ))}
                              </div>
                            )}
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      {/* ---- Running loops not tied to a preset ---- */}
      {orphanRunning.length > 0 && (
        <Panel title="Other running loops" flush live>
          <div className={s.cards}>
            {orphanRunning.map((c) => (
              <LoopCard
                key={c.id}
                campaign={c}
                stats={freshest(stats[c.id], c.loop_stats ?? undefined)}
                onStop={() => stop(c)}
                onCapture={() => setCaptureFor(c)}
              />
            ))}
          </div>
        </Panel>
      )}

      {/* ---- Preset create/edit modal ---- */}
      <Modal
        open={showPreset}
        title={<>{editId != null ? "Edit preset" : "New preset"}</>}
        onClose={() => setShowPreset(false)}
        footer={
          <ModalActions
            onCancel={() => setShowPreset(false)}
            onConfirm={savePreset}
            confirmLabel={editId != null ? "Save" : "Create"}
            disabled={busy}
          />
        }
      >
        <FieldRow>
          <Field label="Preset name" hint="e.g. guinea-orange-1.90">
            <input
              value={form.name}
              onChange={(e) => set("name", e.target.value)}
              placeholder="guinea-orange-1.90"
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

        <div className={s.formSection}>Destination (MADA switch)</div>
        <FieldRow>
          <Field label="Destination host">
            <input
              value={form.dest_host}
              onChange={(e) => set("dest_host", e.target.value)}
              placeholder="208.87.169.100"
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
          <Field label="Duration mode">
            <select
              value={form.duration_mode}
              onChange={(e) =>
                set("duration_mode", e.target.value as LoopPresetRequest["duration_mode"])
              }
            >
              <option value="fixed">Fixed</option>
              <option value="range">Range</option>
            </select>
          </Field>
        </FieldRow>

        <FieldRow>
          <Field
            label={form.duration_mode === "range" ? "Min duration (s)" : "Duration (s) · ACD"}
            hint="ACD in seconds (1.90 min = 114)."
          >
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

        <FieldRow>
          <Field
            label="Media (RTP)"
            hint="None = signaling only (near-zero CPU). Once = stream the PCMA sample at answer. Looped = stream media the whole call (real softswitch load). The UAS echoes it → two-way."
          >
            <select
              value={form.rtp ? (form.rtp_loop ? "loop" : "once") : "off"}
              onChange={(e) => {
                const v = e.target.value;
                set("rtp", v !== "off");
                set("rtp_loop", v === "loop");
              }}
            >
              <option value="off">None — signaling only</option>
              <option value="once">RTP — play once</option>
              <option value="loop">RTP — looped (full call)</option>
            </select>
          </Field>
        </FieldRow>

        {/* ---- Traffic profile (diurnal shaper) ----
           When enabled, a run of this preset steps its rate hourly along a
           day/night curve sized from the daily minutes target + ACD (the
           Duration above). Off = a flat rate (the Call rate above). */}
        <div className={s.formSection}>Traffic profile (diurnal shaper)</div>
        <label
          style={{
            display: "flex", alignItems: "center", gap: 8,
            fontSize: "var(--fs-sm)", cursor: "pointer", margin: "var(--space-2) 0",
          }}
        >
          <input
            type="checkbox"
            checked={!!form.profile_enabled}
            onChange={(e) => set("profile_enabled", e.target.checked)}
          />
          <span style={{ color: "var(--text-bright)" }}>Enable trend</span>
          <span style={{ color: "var(--text-muted)" }}>
            shape this loop's rate to a daily curve (organic-looking traffic)
          </span>
        </label>

        {form.profile_enabled && (
          <>
            <FieldRow>
              <Field label="Preset" hint="curve shape (only diurnal for now)">
                <select
                  value={form.profile_preset}
                  onChange={(e) => set("profile_preset", e.target.value)}
                >
                  <option value="diurnal">Diurnal (day/night)</option>
                </select>
              </Field>
              <Field label="Daily minutes target" hint="rate is sized from this + ACD">
                <input
                  type="number"
                  value={form.target_minutes}
                  onChange={(e) => set("target_minutes", Number(e.target.value))}
                />
              </Field>
              <Field label="Night floor (0–1)" hint="overnight rate vs the daytime peak">
                <input
                  type="number"
                  step="0.05"
                  min="0"
                  max="1"
                  value={form.night_floor}
                  onChange={(e) => set("night_floor", Number(e.target.value))}
                />
              </Field>
            </FieldRow>

            <FieldRow>
              <Field label="Ramp-up start (h)" hint="rise begins, local hour 0–23">
                <input
                  type="number"
                  min="0"
                  max="23"
                  value={form.ramp_up_start}
                  onChange={(e) => set("ramp_up_start", Number(e.target.value))}
                />
              </Field>
              <Field label="Plateau start (h)" hint="reaches the peak">
                <input
                  type="number"
                  min="0"
                  max="23"
                  value={form.plateau_start}
                  onChange={(e) => set("plateau_start", Number(e.target.value))}
                />
              </Field>
              <Field label="Plateau end (h)" hint="peak holds until">
                <input
                  type="number"
                  min="0"
                  max="23"
                  value={form.plateau_end}
                  onChange={(e) => set("plateau_end", Number(e.target.value))}
                />
              </Field>
            </FieldRow>

            <FieldRow>
              <Field label="Ramp-down end (h)" hint="back to night floor by">
                <input
                  type="number"
                  min="0"
                  max="23"
                  value={form.ramp_down_end}
                  onChange={(e) => set("ramp_down_end", Number(e.target.value))}
                />
              </Field>
              <Field label="TZ offset (h)" hint="rotate curve to the market's local time">
                <input
                  type="number"
                  value={form.tz_offset}
                  onChange={(e) => set("tz_offset", Number(e.target.value))}
                />
              </Field>
            </FieldRow>

            {/* 24-bar preview of the sized per-hour CPS (peak = the Call rate). */}
            <div
              style={{
                fontSize: "var(--fs-2xs)", textTransform: "uppercase",
                letterSpacing: "var(--tracking-wide)", color: "var(--text-faint)",
                margin: "var(--space-3) 0 4px",
              }}
            >
              Per-hour CPS preview (00–23h)
            </div>
            {profilePreview ? (
              <>
                <CpsSparkline cps={profilePreview.per_hour.map((h) => h.cps)} />
                <dl className={s.kv} style={{ marginTop: "var(--space-2)" }}>
                  <dt>Peak / avg CPS</dt>
                  <dd>
                    {num(profilePreview.peak_cps)} / {num(profilePreview.avg_cps)}
                  </dd>
                  <dt>Peak concurrent</dt>
                  <dd>{int(profilePreview.peak_concurrent)}</dd>
                </dl>
              </>
            ) : (
              <div style={{ fontSize: "var(--fs-xs)", color: "var(--text-muted)" }}>
                Set a daily minutes target to preview the curve.
              </div>
            )}
          </>
        )}
      </Modal>

      {/* ---- Traffic calculator modal (size CPS + concurrency) ---- */}
      <Modal
        open={showCalc}
        title={<><IconWave /> Traffic calculator</>}
        onClose={() => setShowCalc(false)}
        footer={
          <ModalActions
            onCancel={() => setShowCalc(false)}
            onConfirm={runCalc}
            confirmLabel="Calculate"
            disabled={calcBusy}
          />
        }
      >
        <p className={s.advancedSummary}>
          Size a diurnal campaign: from a daily minutes target + ACD + a day/night
          curve, get the peak CPS and concurrency to provision (assumes ~100% answer).
        </p>

        <FieldRow>
          <Field label="Daily minutes target" hint="total answered minutes/day">
            <input
              type="number"
              value={calc.target_minutes}
              onChange={(e) => setCalc((c) => ({ ...c, target_minutes: Number(e.target.value) }))}
            />
          </Field>
          <Field label="ACD (s)" hint="avg call duration in seconds">
            <input
              type="number"
              value={calc.acd_s}
              onChange={(e) => setCalc((c) => ({ ...c, acd_s: Number(e.target.value) }))}
            />
          </Field>
          <Field label="Night floor (0–1)" hint="overnight rate vs the daytime peak">
            <input
              type="number"
              step="0.05"
              min="0"
              max="1"
              value={calc.night_floor}
              onChange={(e) => setCalc((c) => ({ ...c, night_floor: Number(e.target.value) }))}
            />
          </Field>
        </FieldRow>

        {calcRes && (
          <>
            <div className={s.tiles} style={{ gridTemplateColumns: "1fr 1fr 1fr", gap: "var(--space-3)", marginTop: "var(--space-3)" }}>
              <div className={s.mini}>
                <span className={s.miniVal} style={{ color: "var(--signal)" }}>{num(calcRes.peak_cps)}</span>
                <span className={s.miniLabel}>Peak CPS</span>
              </div>
              <div className={s.mini}>
                <span className={s.miniVal}>{num(calcRes.avg_cps)}</span>
                <span className={s.miniLabel}>Avg CPS</span>
              </div>
              <div className={s.mini}>
                <span className={s.miniVal} style={{ color: "var(--cyan)" }}>{int(calcRes.peak_concurrent)}</span>
                <span className={s.miniLabel}>Peak concurrent</span>
              </div>
            </div>

            {/* 24-bar diurnal sparkline of per-hour CPS (inline, no chart lib). */}
            <div
              style={{
                fontSize: "var(--fs-2xs)", textTransform: "uppercase",
                letterSpacing: "var(--tracking-wide)", color: "var(--text-faint)",
                margin: "var(--space-3) 0 4px",
              }}
            >
              Per-hour CPS (00–23h)
            </div>
            <CpsSparkline cps={calcRes.per_hour.map((h) => h.cps)} />

            <dl className={s.kv} style={{ marginTop: "var(--space-3)" }}>
              <dt>Attempts / day</dt>
              <dd>{int(calcRes.attempts_per_day)}</dd>
              <dt>Nodes needed</dt>
              <dd>{int(calcRes.nodes_needed)}</dd>
            </dl>

            {calcRes.warnings.length > 0 && (
              <div style={{ marginTop: "var(--space-2)", display: "grid", gap: 4 }}>
                {calcRes.warnings.map((w, i) => (
                  <div
                    key={i}
                    style={{
                      fontSize: "var(--fs-xs)", color: "var(--amber)",
                      padding: "4px 8px", borderRadius: "var(--r-sm)",
                      border: "1px solid var(--amber)", background: "var(--bg-inset)",
                    }}
                  >
                    {w}
                  </div>
                ))}
              </div>
            )}

            <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "var(--space-4)" }}>
              <Button size="sm" variant="primary" onClick={applyCalcToPreset}>
                <IconPlus /> Apply to new preset
              </Button>
            </div>
          </>
        )}
      </Modal>

      {/* ---- Run modal (pick node or group) ---- */}
      {runFor && (
        <RunModal
          preset={runFor}
          onClose={() => setRunFor(null)}
          onRan={() => {
            setRunFor(null);
            loops.refetch();
          }}
        />
      )}

      {/* ---- Trace-capture modal (per running loop) ---- */}
      {captureFor && (
        <CaptureModal campaign={captureFor} onClose={() => setCaptureFor(null)} />
      )}
    </>
  );
}
