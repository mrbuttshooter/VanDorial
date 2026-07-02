import { describe, expect, it } from "vitest";
import type { LoopStats } from "@/lib/types";
import { NER_FAIL_CODES, acd, asr, bytes, minutes, ner, networkFails } from "./loopsUtils";

/* Locks the extracted loop math (there was no test before the split). */

function stats(over: Partial<LoopStats> = {}): LoopStats {
  return {
    campaign_id: "c",
    ts: "2026-07-02T00:00:00Z",
    calls_out: 0,
    answered_out: 0,
    minutes_out_ms: 0,
    calls_in_matched: 0,
    minutes_in_ms: 0,
    completion_pct: 0,
    delta_avg_ms: 0,
    delta_p50_ms: 0,
    delta_p95_ms: 0,
    failures: { out: {}, in: {} },
    delta_histogram: [],
    unmatched_pairs: [],
    ...over,
  };
}

describe("loopsUtils", () => {
  it("minutes converts ms and guards null/NaN", () => {
    expect(minutes(60000)).toBe(1);
    expect(minutes(null)).toBe(0);
    expect(minutes(undefined)).toBe(0);
    expect(minutes(NaN)).toBe(0);
  });

  it("bytes formats scale suffixes", () => {
    expect(bytes(null)).toBe("—");
    expect(bytes(512)).toBe("512 B");
    expect(bytes(2048)).toBe("2.0 KB");
    expect(bytes(5 * 1024 * 1024)).toBe("5.0 MB");
  });

  it("asr is answered/originated as a percentage", () => {
    expect(asr(stats({ calls_out: 200, answered_out: 100 }))).toBe(50);
    expect(asr(stats({ calls_out: 0 }))).toBe(0);
  });

  it("acd is minutes_out/answered in seconds", () => {
    // 10 answered calls totalling 600s -> 60s ACD.
    expect(acd(stats({ answered_out: 10, minutes_out_ms: 600_000 }))).toBe(60);
    expect(acd(stats({ answered_out: 0 }))).toBe(0);
  });

  it("networkFails counts only NER_FAIL_CODES", () => {
    // 404/503 count; 486 (busy) and 603 (decline) are NER-neutral.
    const failures = { "404": 3, "503": 2, "486": 5, "603": 9 };
    expect(networkFails(failures)).toBe(5);
    expect([...NER_FAIL_CODES]).toContain("404");
    expect(NER_FAIL_CODES.has("486")).toBe(false);
  });

  it("ner subtracts only network failures from originated", () => {
    // 100 out, 10 network fails -> 90% routed; busy/no-answer don't count.
    const st = stats({ calls_out: 100, failures: { out: { "404": 10, "486": 40 }, in: {} } });
    expect(ner(st)).toBe(90);
    expect(ner(stats({ calls_out: 0 }))).toBe(0);
  });
});
