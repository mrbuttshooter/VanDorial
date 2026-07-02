import { num } from "@/lib/format";
import type { LoopCampaign, LoopPresetRequest, LoopStats } from "@/lib/types";

/* Pure helpers extracted from Loops.tsx (no behavior change). Kept together so
   the page and its extracted components share one source of truth. */

/* A preset is the loop "recipe" — destination + ACD/rate/targets, no source.
   You pick the node or group to fire it on at Run time. */
export const PRESET_BLANK: LoopPresetRequest = {
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
  rtp_loop: false,
  // Diurnal traffic profile (off by default) — the make_curve knobs match the
  // backend defaults so an untouched form posts the same shape the API assumes.
  profile_enabled: false,
  profile_preset: "diurnal",
  night_floor: 0.25,
  ramp_up_start: 6,
  plateau_start: 9,
  plateau_end: 18,
  ramp_down_end: 22,
  tz_offset: 0,
};

/** ms → minutes, rounded to 1 decimal. */
export function minutes(ms: number | null | undefined): number {
  if (ms == null || Number.isNaN(ms)) return 0;
  return ms / 60000;
}

/** Bytes → "0 B" / "4.0 KB" / "12.3 MB" (capture file sizes; no @/lib/format
 *  helper for this yet). */
export function bytes(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  if (n < 1024) return `${n} B`;
  const kb = n / 1024;
  if (kb < 1024) return `${num(kb, 1)} KB`;
  const mb = kb / 1024;
  if (mb < 1024) return `${num(mb, 1)} MB`;
  return `${num(mb / 1024, 1)} GB`;
}

/** ASR: answered ÷ originated, as a 0–100 percentage. */
export function asr(st: LoopStats): number {
  return st.calls_out > 0 ? (st.answered_out / st.calls_out) * 100 : 0;
}

/** ACD: average answered-call duration in seconds (minutes_out ÷ answered). */
export function acd(st: LoopStats): number {
  return st.answered_out > 0 ? st.minutes_out_ms / st.answered_out / 1000 : 0;
}

/** SIP codes that count AGAINST NER — network/route/congestion failures
 *  (no-route = CAU_NO_RT_DST = 404, plus 408/5xx). Every other non-2xx
 *  (486 busy, 480/408 no-answer, 600/603 decline) is NER-neutral: the network
 *  delivered the call, the callee just didn't answer. */
export const NER_FAIL_CODES = new Set(["404", "408", "500", "502", "503", "504"]);

/** Count of a campaign's outbound legs that failed for a network cause. */
export function networkFails(failuresOut: Record<string, number>): number {
  let n = 0;
  for (const [code, c] of Object.entries(failuresOut || {})) {
    if (NER_FAIL_CODES.has(code)) n += c;
  }
  return n;
}

/** NER: (originated − network failures) ÷ originated, 0–100. 100% = the network
 *  routed every call; only no-route/congestion drags it down (not busy/no-ans). */
export function ner(st: LoopStats): number {
  if (!st.calls_out) return 0;
  return ((st.calls_out - networkFails(st.failures?.out ?? {})) / st.calls_out) * 100;
}

/** Pick the FRESHER of the live WS snapshot vs the REST-poll snapshot by ``ts``.
 *  The WS value used to win unconditionally (``ws ?? rest``), so once the socket
 *  delivered one snapshot the card was pinned to it — and when the socket later
 *  went silent (e.g. after a worker restart) the card froze, ignoring the still-
 *  updating 3 s REST poll. Comparing ts lets whichever source is actually fresh
 *  drive the card, so REST keeps it live even with the WS down. */
export function freshest(a?: LoopStats, b?: LoopStats): LoopStats | undefined {
  if (!a) return b;
  if (!b) return a;
  return (a.ts ?? "") >= (b.ts ?? "") ? a : b;
}

/** Loop completion as a fraction toward a calls/minutes target (0–100), or
 *  null when the campaign runs until stopped (no target). */
export function targetProgress(c: LoopCampaign, st: LoopStats | undefined): number | null {
  if (c.target_calls && c.target_calls > 0) {
    const done = st?.calls_out ?? 0;
    return Math.min(100, (done / c.target_calls) * 100);
  }
  if (c.target_minutes && c.target_minutes > 0) {
    // Daily target: progress is minutes done since 00:00 GMT today (resets each
    // GMT day), not lifetime. Falls back to lifetime on older worker payloads.
    const done = minutes(st?.minutes_out_today_ms ?? st?.minutes_out_ms);
    return Math.min(100, (done / c.target_minutes) * 100);
  }
  return null;
}
