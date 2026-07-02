import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import s from "../pages.module.css";
import { Modal, ModalActions } from "@/components/ui/Modal";
import { Field } from "@/components/ui/Misc";
import { IconPlay } from "@/components/icons";
import { useAsync } from "@/hooks/useAsync";
import { api } from "@/lib/api";
import { useToast } from "@/components/ui/Toast";
import { duration } from "@/lib/format";
import type { LoopPreset, RunPresetRequest } from "@/lib/types";

/* Run modal: choose where to fire the preset. Extracted verbatim from Loops.tsx. */
export function RunModal({
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
