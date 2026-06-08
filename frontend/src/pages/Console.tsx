import { useEffect, useRef, useState } from "react";
import s from "./pages.module.css";
import c from "./console.module.css";
import { Panel } from "@/components/ui/Panel";
import { Button } from "@/components/ui/Button";
import { useStream } from "@/hooks/useStream";
import { clock } from "@/lib/format";
import type { LogLine } from "@/lib/types";

type Level = LogLine["level"];
const LEVELS: ("ALL" | Level)[] = ["ALL", "INFO", "WARN", "ERROR", "DEBUG"];
const MAX = 500;

export function Console() {
  const [lines, setLines] = useState<LogLine[]>([]);
  const [filter, setFilter] = useState<"ALL" | Level>("ALL");
  const [paused, setPaused] = useState(false);
  const pausedRef = useRef(false);
  pausedRef.current = paused;
  const termRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  useStream<LogLine>("logs", (line) => {
    if (pausedRef.current) return;
    setLines((prev) => {
      const next = prev.length >= MAX ? prev.slice(prev.length - MAX + 1) : prev.slice();
      next.push(line);
      return next;
    });
  });

  // Autoscroll only when the user is already near the bottom.
  useEffect(() => {
    const el = termRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [lines]);

  const onScroll = () => {
    const el = termRef.current;
    if (!el) return;
    stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  };

  const shown = filter === "ALL" ? lines : lines.filter((l) => l.level === filter);

  return (
    <>
      <div className={s.toolbar}>
        <div className={s.seg}>
          {LEVELS.map((lv) => (
            <button
              key={lv}
              className={`${s.segBtn} ${filter === lv ? s.segActive : ""}`}
              onClick={() => setFilter(lv)}
            >
              {lv}
            </button>
          ))}
        </div>
        <div className={s.spacer} />
        <span className="hud-label">{shown.length} lines</span>
        <Button size="sm" variant={paused ? "primary" : "ghost"} onClick={() => setPaused((p) => !p)}>
          {paused ? "Resume" : "Pause"}
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setLines([])}>
          Clear
        </Button>
      </div>

      <Panel title="Event Stream" live flush>
        <div className={c.term} ref={termRef} onScroll={onScroll}>
          {shown.length === 0 ? (
            <div style={{ padding: "var(--space-4)", color: "var(--text-faint)" }}>
              waiting for events
              <span className={c.cursor} />
            </div>
          ) : (
            shown.map((l, i) => (
              <div key={i} className={`${c.line} ${c[`row_${l.level}`] ?? ""}`}>
                <span className={c.ts}>{clock(l.ts)}</span>
                <span className={`${c.lvl} ${c[`l_${l.level}`]}`}>{l.level}</span>
                <span className={c.src}>{l.source}</span>
                <span className={c.msg}>{l.message}</span>
              </div>
            ))
          )}
        </div>
      </Panel>
    </>
  );
}
