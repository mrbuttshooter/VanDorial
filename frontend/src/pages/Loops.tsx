import { Fragment, useMemo, useState } from "react";
import s from "./pages.module.css";
import ui from "@/components/ui/ui.module.css";
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
  IconTrash,
} from "@/components/icons";
import { useAsync } from "@/hooks/useAsync";
import { useStream } from "@/hooks/useStream";
import { api } from "@/lib/api";
import { useToast } from "@/components/ui/Toast";
import { duration, int, num, pct } from "@/lib/format";
import { Link } from "react-router-dom";
import type {
  LoopCampaign,
  LoopPreset,
  LoopPresetRequest,
  LoopStats,
  RunPresetRequest,
  Transport,
} from "@/lib/types";

/* A preset is the loop "recipe" — destination + ACD/rate/targets, no source.
   You pick the node or group to fire it on at Run time. */
const PRESET_BLANK: LoopPresetRequest = {
  name: "",
  description: "",
  dest_host: "",
  dest_port: 5060,
  transport: "udp",
  rate: 1,
  max_concurrent: 10,
  duration_mode: "fixed",
  duration_s: 114,
  duration_max_s: 0,
  match_key: "exact",
  target_calls: 0,
  target_minutes: 0,
  rtp: false,
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

  // Run modal (pick node or group for a chosen preset).
  const [runFor, setRunFor] = useState<LoopPreset | null>(null);

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

  const download = async (id: string, box?: string) => {
    try {
      await api.downloadLoopRecordsCsv(id, box);
    } catch (e) {
      toast.error(`Download failed: ${e instanceof Error ? e.message : e}`);
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
  // A running campaign belongs to a preset if its name matches "<preset>" or
  // "<preset>-<node>" (how preset runs are named).
  const runsForPreset = (p: LoopPreset) =>
    running.filter((c) => c.name === p.name || c.name.startsWith(`${p.name}-`));
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
                              title="Streams RTP media (PCMA)"
                              style={{
                                marginLeft: 8, fontSize: "0.7em", fontWeight: 600,
                                color: "var(--cyan)", border: "1px solid var(--cyan)",
                                borderRadius: 3, padding: "0 4px", verticalAlign: "middle",
                              }}
                            >
                              RTP
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
                                    stats={stats[c.id] ?? c.loop_stats ?? undefined}
                                    onStop={() => stop(c)}
                                    onDownload={() => download(c.id, c.box)}
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
                stats={stats[c.id] ?? c.loop_stats ?? undefined}
                onStop={() => stop(c)}
                onDownload={() => download(c.id, c.box)}
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
            label="Media"
            hint="On = stream real RTP audio (PCMA) every call (the UAS echoes it → two-way). Off = signaling only, no media on the wire (near-zero CPU)."
          >
            <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
              <input
                type="checkbox"
                checked={!!form.rtp}
                onChange={(e) => set("rtp", e.target.checked)}
                style={{ width: "auto" }}
              />
              <span>RTP media</span>
            </label>
          </Field>
        </FieldRow>
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
    </>
  );
}

/* ---- Run modal: choose where to fire the preset --------------------------- */

function RunModal({
  preset,
  onClose,
  onRan,
}: {
  preset: LoopPreset;
  onClose: () => void;
  onRan: () => void;
}) {
  const nodes = useAsync(() => api.listServers(), []);
  const groups = useAsync(() => api.listNodeGroups(), []);
  const toast = useToast();
  const [mode, setMode] = useState<"node" | "group">("node");
  const [nodeId, setNodeId] = useState<number | undefined>(undefined);
  const [groupId, setGroupId] = useState<number | undefined>(undefined);
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState(false);

  const usableNodes = useMemo(
    () => (nodes.data?.servers ?? []).filter((n) => n.enabled && n.has_pool),
    [nodes.data],
  );
  const groupRows = groups.data?.groups ?? [];
  const selectedGroup = groupRows.find((g) => g.id === groupId);
  const groupMembers = useMemo(
    () => (selectedGroup?.nodes ?? []).filter((m) => m.enabled && m.has_pool),
    [selectedGroup],
  );

  // Choosing a group pre-selects ALL its runnable members; uncheck to skip.
  const chooseGroup = (gid: number | undefined) => {
    setGroupId(gid);
    const g = groupRows.find((x) => x.id === gid);
    setPicked(new Set((g?.nodes ?? []).filter((m) => m.enabled && m.has_pool).map((m) => m.id)));
  };
  const toggle = (id: number) =>
    setPicked((p) => {
      const n = new Set(p);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  const run = async () => {
    let target: RunPresetRequest;
    if (mode === "node") {
      if (!nodeId) { toast.error("Pick a node."); return; }
      target = { node_id: nodeId };
    } else {
      if (!groupId) { toast.error("Pick a group."); return; }
      if (picked.size === 0) { toast.error("Select at least one node in the group."); return; }
      target = { group_id: groupId, node_ids: [...picked] };
    }
    setBusy(true);
    try {
      const res = await api.runLoopPreset(preset.id, target);
      const failed = res.results.filter((r) => !r.ok);
      if (res.started > 0) {
        toast.ok(`Started ${res.started}/${res.total} · ${preset.name}`);
      }
      if (failed.length) {
        toast.warn(
          `${failed.length} not started: ${failed
            .map((r) => `${r.ip} ${r.skipped ?? r.error ?? ""}`)
            .join("; ")}`,
        );
      }
      onRan();
    } catch (e) {
      toast.error(`Run failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open
      title={<><IconPlay /> Run · {preset.name}</>}
      onClose={onClose}
      footer={
        <ModalActions onCancel={onClose} onConfirm={run} confirmLabel="Run" disabled={busy} />
      }
    >
      <p className={s.advancedSummary}>
        {preset.dest_host}:{preset.dest_port} · ACD {duration(preset.duration_s)} · {preset.rate} cps
      </p>

      <div className={s.seg} style={{ marginBottom: "var(--space-3)" }}>
        <button
          className={`${s.segBtn} ${mode === "node" ? s.segActive : ""}`}
          onClick={() => setMode("node")}
        >
          One node
        </button>
        <button
          className={`${s.segBtn} ${mode === "group" ? s.segActive : ""}`}
          onClick={() => setMode("group")}
        >
          A group
        </button>
      </div>

      {mode === "node" ? (
        <Field
          label="Source-IP node"
          hint={usableNodes.length ? "One loop per IP." : "No nodes with a pool yet."}
        >
          <select
            value={nodeId ?? ""}
            onChange={(e) => setNodeId(e.target.value ? Number(e.target.value) : undefined)}
          >
            <option value="">Select node</option>
            {usableNodes.map((n) => (
              <option key={n.id} value={n.id}>
                {n.name} — {n.ip} · {n.origin_zone} → {n.dest_zone}
              </option>
            ))}
          </select>
        </Field>
      ) : (
        <>
          <Field
            label="Group"
            hint={groupRows.length ? "Pick which member IPs below." : "No groups yet."}
          >
            <select
              value={groupId ?? ""}
              onChange={(e) => chooseGroup(e.target.value ? Number(e.target.value) : undefined)}
            >
              <option value="">Select group</option>
              {groupRows.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name} — {g.node_count ?? 0} nodes
                </option>
              ))}
            </select>
          </Field>

          {selectedGroup && (
            <Field
              label={`Run on which IPs (${picked.size}/${groupMembers.length})`}
              hint="All selected — uncheck any you don't want to run."
            >
              {groupMembers.length === 0 ? (
                <span style={{ fontSize: "var(--fs-xs)", color: "var(--text-muted)" }}>
                  No member nodes with a pool.
                </span>
              ) : (
                <div
                  style={{
                    display: "grid",
                    gap: 4,
                    maxHeight: 200,
                    overflowY: "auto",
                    border: "1px solid var(--line)",
                    borderRadius: "var(--r-sm)",
                    padding: "var(--space-2)",
                    background: "var(--bg-inset)",
                  }}
                >
                  {groupMembers.map((m) => (
                    <label
                      key={m.id}
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 8,
                        fontSize: "var(--fs-sm)",
                        cursor: "pointer",
                      }}
                    >
                      <input
                        type="checkbox"
                        checked={picked.has(m.id)}
                        onChange={() => toggle(m.id)}
                      />
                      <span style={{ color: "var(--text-bright)" }}>{m.name}</span>
                      <span style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono, monospace)" }}>
                        {m.ip}
                      </span>
                    </label>
                  ))}
                </div>
              )}
            </Field>
          )}
        </>
      )}

      {mode === "node" && usableNodes.length === 0 && (
        <p className={s.advancedSummary}>
          No nodes with a number pool — <Link to="/nodes">add one on the Nodes page</Link>.
        </p>
      )}
    </Modal>
  );
}

/* ---- Per-campaign live card (running loops) ------------------------------- */

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
