import { useMemo, useState } from "react";
import s from "./pages.module.css";
import ui from "@/components/ui/ui.module.css";
import { Panel } from "@/components/ui/Panel";
import { StatTile } from "@/components/ui/StatTile";
import { Badge } from "@/components/ui/Badge";
import { statusTone } from "@/components/ui/tone";
import { EmptyState, Spinner } from "@/components/ui/Misc";
import { useAsync } from "@/hooks/useAsync";
import { api } from "@/lib/api";
import { int, num, pct, duration } from "@/lib/format";
import { networkFails } from "./loops/loopsUtils";
import type { LoopCampaign } from "@/lib/types";

/* Performance = a filterable rollup over every loop in the fleet (this box + all
   remote workers). Filter by source IP, group, or destination; the tiles sum the
   matching loops' accounting (minutes out/in, ASR, ACD, completion). */

function mins(ms: number | null | undefined): number {
  return ms == null || Number.isNaN(ms) ? 0 : ms / 60000;
}

interface Roll {
  callsOut: number; answered: number; matched: number; netFail: number;
  minOut: number; minIn: number; running: number;
}
function rollup(camps: LoopCampaign[]): Roll {
  const r: Roll = { callsOut: 0, answered: 0, matched: 0, netFail: 0, minOut: 0, minIn: 0, running: 0 };
  for (const c of camps) {
    if (c.status === "running") r.running++;
    const ls = c.loop_stats;
    if (!ls) continue;
    r.callsOut += ls.calls_out;
    r.answered += ls.answered_out;
    r.matched += ls.calls_in_matched;
    r.netFail += networkFails(ls.failures?.out ?? {});
    r.minOut += ls.minutes_out_ms;
    r.minIn += ls.minutes_in_ms;
  }
  return r;
}

export function Performance() {
  const loops = useAsync(() => api.listLoopsFleet(), [], 3000);
  const servers = useAsync(() => api.listServers(), []);
  const groups = useAsync(() => api.listNodeGroups(), []);

  const [ip, setIp] = useState("all");
  const [groupName, setGroupName] = useState("all");
  const [dest, setDest] = useState("all");

  const campaigns = useMemo(() => loops.data?.campaigns ?? [], [loops.data]);

  // ip -> group name (from this box's node registry; covers local + remote nodes).
  const ipGroup = useMemo(() => {
    const gname: Record<number, string> = {};
    for (const g of groups.data?.groups ?? []) gname[g.id] = g.name;
    const m: Record<string, string> = {};
    for (const n of servers.data?.servers ?? []) {
      if (n.group_id != null) m[n.ip] = gname[n.group_id] ?? `#${n.group_id}`;
    }
    return m;
  }, [servers.data, groups.data]);

  const ips = useMemo(
    () => Array.from(new Set(campaigns.map((c) => c.local_ip).filter(Boolean))) as string[],
    [campaigns],
  );
  const dests = useMemo(
    () => Array.from(new Set(campaigns.map((c) => c.dest_host).filter(Boolean))),
    [campaigns],
  );
  const groupList = groups.data?.groups ?? [];

  const filtered = useMemo(
    () =>
      campaigns.filter((c) => {
        if (ip !== "all" && (c.local_ip ?? "") !== ip) return false;
        if (dest !== "all" && c.dest_host !== dest) return false;
        if (groupName !== "all" && (ipGroup[c.local_ip ?? ""] ?? "") !== groupName) return false;
        return true;
      }),
    [campaigns, ip, dest, groupName, ipGroup],
  );

  const r = rollup(filtered);
  const asr = r.callsOut ? (r.answered / r.callsOut) * 100 : 0;
  const nerPct = r.callsOut ? ((r.callsOut - r.netFail) / r.callsOut) * 100 : 0;
  const acd = r.answered ? r.minOut / r.answered / 1000 : 0;
  const comp = r.answered ? (r.matched / r.answered) * 100 : 0;

  return (
    <>
      {/* ---- Filters ---- */}
      <div className={s.toolbar}>
        <span className="hud-label">Filter</span>
        <select value={ip} onChange={(e) => setIp(e.target.value)} aria-label="Source IP">
          <option value="all">All source IPs</option>
          {ips.map((x) => <option key={x} value={x}>{x}</option>)}
        </select>
        <select value={groupName} onChange={(e) => setGroupName(e.target.value)} aria-label="Group">
          <option value="all">All groups</option>
          {groupList.map((g) => <option key={g.id} value={g.name}>{g.name}</option>)}
        </select>
        <select value={dest} onChange={(e) => setDest(e.target.value)} aria-label="Destination">
          <option value="all">All destinations</option>
          {dests.map((d) => <option key={d} value={d}>{d}</option>)}
        </select>
        <div className={s.spacer} />
        <span className="hud-label">{filtered.length} loops · {r.running} running</span>
      </div>

      {/* ---- Rollup tiles ---- */}
      <div className={s.tiles}>
        <StatTile label="Running loops" value={int(r.running)} tone="cyan"
          sub={<span className="hud-label">{filtered.length} total</span>} />
        <StatTile label="ASR" value={num(asr, 1)} unit="%"
          tone={asr >= 95 ? "signal" : asr >= 50 ? "amber" : "crit"} />
        <StatTile label="NER" value={num(nerPct, 1)} unit="%"
          tone={nerPct >= 99 ? "signal" : nerPct >= 90 ? "amber" : "crit"} />
        <StatTile label="ACD" value={duration(acd)} tone="signal" />
        <StatTile label="Completion" value={num(comp, 1)} unit="%"
          tone={comp >= 95 ? "signal" : comp >= 80 ? "amber" : "crit"} />
        <StatTile label="Minutes OUT" value={num(mins(r.minOut), 1)} tone="cyan" />
        <StatTile label="Minutes IN" value={num(mins(r.minIn), 1)} tone="cyan" />
      </div>

      {/* ---- Per-loop breakdown ---- */}
      <Panel title="Loops in scope" flush live>
        {loops.loading && !loops.data ? (
          <div style={{ padding: "var(--space-6)", display: "grid", placeItems: "center" }}>
            <Spinner />
          </div>
        ) : filtered.length === 0 ? (
          <EmptyState title="No loops match" hint="Adjust the filters, or start a loop from the Loops page." />
        ) : (
          <div className={ui.tableWrap}>
            <table className={ui.table}>
              <thead>
                <tr>
                  <th>Loop</th>
                  <th>Source IP</th>
                  <th>Group</th>
                  <th>Destination</th>
                  <th className={ui.numCell}>ASR</th>
                  <th className={ui.numCell}>NER</th>
                  <th className={ui.numCell}>ACD</th>
                  <th className={ui.numCell}>Completion</th>
                  <th className={ui.numCell}>Min out/in</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((c) => {
                  const ls = c.loop_stats;
                  const cAsr = ls && ls.calls_out ? (ls.answered_out / ls.calls_out) * 100 : 0;
                  const cNer = ls && ls.calls_out
                    ? ((ls.calls_out - networkFails(ls.failures?.out ?? {})) / ls.calls_out) * 100 : 0;
                  const cAcd = ls && ls.answered_out ? ls.minutes_out_ms / ls.answered_out / 1000 : 0;
                  return (
                    <tr key={c.id}>
                      <td style={{ color: "var(--text-bright)", fontWeight: 600 }}>{c.name}</td>
                      <td style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono, monospace)" }}>
                        {c.local_ip || "—"}
                        {c.box && c.box !== "local" ? <span style={{ color: "var(--cyan)" }}> ⇄</span> : null}
                      </td>
                      <td style={{ color: "var(--text-muted)" }}>{ipGroup[c.local_ip ?? ""] ?? "—"}</td>
                      <td style={{ color: "var(--text-muted)" }}>{c.dest_host}:{c.dest_port}</td>
                      <td className={ui.numCell}>{ls ? pct(cAsr) : "—"}</td>
                      <td className={ui.numCell}
                          style={{ color: ls ? (cNer >= 99 ? "var(--signal)" : cNer >= 90 ? "var(--amber)" : "var(--crit)") : undefined }}>
                        {ls ? pct(cNer) : "—"}
                      </td>
                      <td className={ui.numCell}>{ls ? duration(cAcd) : "—"}</td>
                      <td className={ui.numCell}>{ls ? pct(ls.completion_pct) : "—"}</td>
                      <td className={ui.numCell}>
                        {num(mins(ls?.minutes_out_ms))} / {num(mins(ls?.minutes_in_ms))}
                      </td>
                      <td><Badge tone={statusTone(c.status)} pulse={c.status === "running"}>{c.status}</Badge></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </>
  );
}
