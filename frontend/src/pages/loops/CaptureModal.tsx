import { useState } from "react";
import s from "../pages.module.css";
import ui from "@/components/ui/ui.module.css";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Spinner } from "@/components/ui/Misc";
import { IconPlay, IconStop, IconWave, IconDownload, IconTrash } from "@/components/icons";
import { useAsync } from "@/hooks/useAsync";
import { api } from "@/lib/api";
import { useToast } from "@/components/ui/Toast";
import { clock } from "@/lib/format";
import type { CaptureInfo, LoopCampaign } from "@/lib/types";
import { bytes } from "./loopsUtils";

/* Trace-capture modal: start/stop tcpdump for one running loop and pull its
   .pcap on demand. The capture runs on the loop's box (worker), so we pass
   `box = campaign.box ?? "local"` to every fleet-capture call. Extracted
   verbatim from Loops.tsx. */
export function CaptureModal({
  campaign,
  onClose,
}: {
  campaign: LoopCampaign;
  onClose: () => void;
}) {
  const box = campaign.box ?? "local";
  // Poll so a running capture's size grows live.
  const caps = useAsync(() => api.listCaptures(campaign.id, box), [campaign.id], 2000);
  const toast = useToast();
  const [busy, setBusy] = useState(false);

  const rows = caps.data?.captures ?? [];
  const anyRunning = rows.some((c) => c.running);

  const startCap = async () => {
    setBusy(true);
    try {
      await api.startCapture(campaign.id, box);
      toast.ok("Capture started");
      caps.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  };

  const stopCap = async (cap: CaptureInfo) => {
    setBusy(true);
    try {
      await api.stopCapture(campaign.id, box, cap.id);
      toast.warn("Capture stopped");
      caps.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  };

  const download = async (cap: CaptureInfo) => {
    try {
      await api.downloadCapture(campaign.id, box, cap.id);
    } catch (e) {
      toast.error(`Download failed: ${e instanceof Error ? e.message : e}`);
    }
  };

  const del = async (cap: CaptureInfo) => {
    try {
      await api.deleteCapture(campaign.id, box, cap.id);
      toast.warn("Capture deleted");
      caps.refetch();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
    }
  };

  return (
    <Modal
      open
      title={<><IconWave /> Trace · {campaign.name}</>}
      onClose={onClose}
      footer={
        <Button variant="ghost" onClick={onClose}>
          Close
        </Button>
      }
    >
      <p className={s.advancedSummary}>
        Capture packets to/from {campaign.dest_host}:{campaign.dest_port} on{" "}
        {box === "local" ? "this box" : box.replace(/^https?:\/\//, "")}. The .pcap stays on
        the worker until you download or delete it.
      </p>

      <div style={{ display: "flex", gap: "var(--space-2)", marginBottom: "var(--space-3)" }}>
        <Button size="sm" variant="primary" onClick={startCap} disabled={busy}>
          <IconPlay /> Start capture
        </Button>
      </div>

      {caps.loading && !caps.data ? (
        <div style={{ padding: "var(--space-4)", display: "grid", placeItems: "center" }}>
          <Spinner />
        </div>
      ) : rows.length === 0 ? (
        <div style={{ fontSize: "var(--fs-xs)", color: "var(--text-muted)" }}>
          No captures yet — hit <strong>Start capture</strong> to record this loop's packets.
        </div>
      ) : (
        <div className={ui.tableWrap}>
          <table className={ui.table}>
            <thead>
              <tr>
                <th>State</th>
                <th className={ui.numCell}>Size</th>
                <th>Started</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((cap) => (
                <tr key={cap.id}>
                  <td>
                    {cap.running ? (
                      <Badge tone="signal" pulse>recording</Badge>
                    ) : (
                      <span style={{ color: "var(--text-faint)" }}>stopped</span>
                    )}
                  </td>
                  <td className={ui.numCell}>{bytes(cap.size_bytes)}</td>
                  <td style={{ color: "var(--text-muted)" }}>
                    {cap.started_at != null ? clock(cap.started_at) : "—"}
                  </td>
                  <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                    {cap.running && (
                      <Button size="sm" variant="ghost" onClick={() => stopCap(cap)} disabled={busy}>
                        <IconStop /> Stop
                      </Button>
                    )}
                    <Button
                      size="sm"
                      variant="ghost"
                      icon
                      title="Download .pcap"
                      onClick={() => download(cap)}
                      disabled={cap.running}
                    >
                      <IconDownload />
                    </Button>
                    <Button size="sm" variant="ghost" icon title="Delete capture" onClick={() => del(cap)}>
                      <IconTrash />
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {anyRunning && (
        <p style={{ fontSize: "var(--fs-2xs)", color: "var(--text-faint)", marginTop: "var(--space-3)" }}>
          Stop a capture before downloading it. Captures auto-stop at the worker's size/duration
          cap; delete traces you don't need.
        </p>
      )}
    </Modal>
  );
}
