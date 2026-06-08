/* ============================================================================
   Live stream client for gencall/api/websocket.py.

   Subscribes to topic streams over a single multiplexed /ws connection with
   auto-reconnect + backoff. In mock mode it synthesizes the same stream events
   from the in-browser simulator, so consumers don't care which is active.
   ============================================================================ */
import type { LogLine, StatsSnapshot, StreamMessage, StreamTopic } from "./types";
import { MOCK_ENABLED, mockApi } from "./mock";

type Handler<T = unknown> = (data: T) => void;
type StatusHandler = (connected: boolean) => void;

class StreamClient {
  private ws: WebSocket | null = null;
  private handlers = new Map<StreamTopic, Set<Handler>>();
  private statusHandlers = new Set<StatusHandler>();
  private wanted = new Set<StreamTopic>();
  private backoff = 1000;
  private connected = false;
  private mockTimer: number | null = null;
  private closedByUser = false;

  /** Subscribe to a topic. Returns an unsubscribe fn. */
  on<T>(topic: StreamTopic, handler: Handler<T>): () => void {
    if (!this.handlers.has(topic)) this.handlers.set(topic, new Set());
    this.handlers.get(topic)!.add(handler as Handler);
    this.wanted.add(topic);
    this.ensure();
    this.sendSubscribe();
    return () => {
      this.handlers.get(topic)?.delete(handler as Handler);
    };
  }

  onStatus(handler: StatusHandler): () => void {
    this.statusHandlers.add(handler);
    handler(this.connected);
    return () => this.statusHandlers.delete(handler);
  }

  private emit(topic: StreamTopic, data: unknown) {
    this.handlers.get(topic)?.forEach((h) => h(data));
  }

  private setConnected(v: boolean) {
    if (this.connected === v) return;
    this.connected = v;
    this.statusHandlers.forEach((h) => h(v));
  }

  private ensure() {
    if (MOCK_ENABLED) {
      this.startMock();
      return;
    }
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING))
      return;
    this.connect();
  }

  private connect() {
    this.closedByUser = false;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}/ws`;
    try {
      this.ws = new WebSocket(url);
    } catch {
      this.scheduleReconnect();
      return;
    }

    this.ws.onopen = () => {
      this.backoff = 1000;
      this.setConnected(true);
      this.sendSubscribe();
    };
    this.ws.onmessage = (ev) => {
      let msg: StreamMessage;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (msg.type === "stream" && msg.topic) this.emit(msg.topic, msg.data);
    };
    this.ws.onclose = () => {
      this.setConnected(false);
      if (!this.closedByUser) this.scheduleReconnect();
    };
    this.ws.onerror = () => this.ws?.close();
  }

  private scheduleReconnect() {
    this.setConnected(false);
    window.setTimeout(() => this.ensure(), this.backoff);
    this.backoff = Math.min(this.backoff * 1.7, 15000);
  }

  private sendSubscribe() {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify({ action: "subscribe", topics: [...this.wanted] }));
  }

  // ---- Mock: drive streams from the in-browser simulator ----------------
  private startMock() {
    if (this.mockTimer != null) return;
    this.setConnected(true);
    const sources = ["sipp.uac", "stats.engine", "core.cdr", "ws.hub", "scheduler"];
    const levels: LogLine["level"][] = ["INFO", "INFO", "INFO", "DEBUG", "WARN"];
    this.mockTimer = window.setInterval(async () => {
      const s: StatsSnapshot = await mockApi.stats();
      this.emit("stats", s);
      if (Math.random() < 0.7) {
        const lvl = levels[Math.floor(Math.random() * levels.length)];
        const line: LogLine = {
          ts: Date.now() / 1000,
          level: lvl,
          source: sources[Math.floor(Math.random() * sources.length)],
          message: this.fakeLog(lvl, s),
        };
        this.emit("logs", line);
      }
    }, 1000) as unknown as number;
  }

  private fakeLog(level: LogLine["level"], s: StatsSnapshot): string {
    if (level === "WARN")
      return `retransmission spike on leg edge-soak (rt=${s.avg_response_time_ms.toFixed(0)}ms)`;
    const msgs = [
      `aggregate cps=${s.calls_per_second.toFixed(1)} active=${s.active_instances}`,
      `200 OK received, dialog established (calls=${s.total_calls})`,
      `stats snapshot flushed to ring buffer`,
      `ACK sent → media path open`,
      `BYE 200 OK, call torn down cleanly`,
    ];
    return msgs[Math.floor(Math.random() * msgs.length)];
  }

  close() {
    this.closedByUser = true;
    this.ws?.close();
    if (this.mockTimer != null) {
      clearInterval(this.mockTimer);
      this.mockTimer = null;
    }
  }
}

export const stream = new StreamClient();
