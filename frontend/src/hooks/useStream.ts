import { useEffect, useRef, useState } from "react";
import { stream } from "@/lib/ws";
import type { StatsSnapshot, StreamTopic } from "@/lib/types";

/** Subscribe to a live topic; the callback ref stays current without resubscribing. */
export function useStream<T>(topic: StreamTopic, onData: (data: T) => void) {
  const cb = useRef(onData);
  cb.current = onData;
  useEffect(() => stream.on<T>(topic, (d) => cb.current(d)), [topic]);
}

/** Live websocket connection status (true once the hub is connected). */
export function useStreamStatus(): boolean {
  const [connected, setConnected] = useState(false);
  useEffect(() => stream.onStatus(setConnected), []);
  return connected;
}

/**
 * Maintain a rolling window of stats snapshots fed by the `stats` stream,
 * plus the latest value. `seed` pre-fills the buffer (e.g. from REST history).
 */
export function useLiveStats(capacity = 180) {
  const [latest, setLatest] = useState<StatsSnapshot | null>(null);
  const [series, setSeries] = useState<StatsSnapshot[]>([]);

  useStream<StatsSnapshot>("stats", (s) => {
    setLatest(s);
    setSeries((prev) => {
      const next = prev.length >= capacity ? prev.slice(prev.length - capacity + 1) : prev.slice();
      next.push(s);
      return next;
    });
  });

  const seed = (snapshots: StatsSnapshot[]) => {
    setSeries(snapshots.slice(-capacity));
    if (snapshots.length) setLatest(snapshots[snapshots.length - 1]);
  };

  return { latest, series, seed };
}
