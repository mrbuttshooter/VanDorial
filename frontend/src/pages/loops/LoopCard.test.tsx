import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import type { LoopCampaign, LoopStats } from "@/lib/types";
import { LoopCard } from "./LoopCard";

/* Render smoke test for the extracted card — locks that the split component
   mounts and shows the campaign's live figures. */

const campaign: LoopCampaign = {
  id: "loop-1",
  name: "algeria-mobile",
  status: "running",
  node_id: 1,
  local_ip: "10.0.0.11",
  dest_host: "203.0.113.9",
  dest_port: 5060,
  transport: "udp",
  csv_path: "",
  rate: 5,
  max_concurrent: 10,
  duration_mode: "fixed",
  duration_s: 114,
  duration_max_s: 0,
  match_key: "exact",
  target_calls: 0,
  target_minutes: 0,
  created_at: null,
  started_at: null,
  stopped_at: null,
};

const stats: LoopStats = {
  campaign_id: "loop-1",
  ts: "2026-07-02T00:00:00Z",
  calls_out: 200,
  answered_out: 100,
  minutes_out_ms: 600_000,
  calls_in_matched: 90,
  minutes_in_ms: 590_000,
  completion_pct: 90,
  delta_avg_ms: 1000,
  delta_p50_ms: 900,
  delta_p95_ms: 1500,
  failures: { out: { "404": 4 }, in: {} },
  delta_histogram: [],
  unmatched_pairs: [],
};

describe("LoopCard", () => {
  it("renders the campaign name, route and computed ASR", () => {
    render(<LoopCard campaign={campaign} stats={stats} onStop={() => {}} onCapture={() => {}} />);
    expect(screen.getByText("algeria-mobile")).toBeInTheDocument();
    expect(screen.getByText(/203\.0\.113\.9:5060/)).toBeInTheDocument();
    // ASR = 100/200 = 50% (pct renders one decimal).
    expect(screen.getByText("50.0%")).toBeInTheDocument();
    // The failing SIP code is listed.
    expect(screen.getByText("404")).toBeInTheDocument();
  });

  it("shows placeholders when no stats snapshot is available", () => {
    render(<LoopCard campaign={campaign} stats={undefined} onStop={() => {}} onCapture={() => {}} />);
    expect(screen.getByText("algeria-mobile")).toBeInTheDocument();
    // No outbound failures line when stats is absent.
    expect(screen.getByText("No outbound failures.")).toBeInTheDocument();
  });
});
