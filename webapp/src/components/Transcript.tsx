import * as React from "react";
import type { Row } from "@/lib/transcript";
import { UserRowView } from "./rows/UserRowView";
import { AssistantRowView } from "./rows/AssistantRowView";
import { ThinkingRowView } from "./rows/ThinkingRowView";
import { ToolRowView } from "./rows/ToolRowView";
import { NoticeRowView } from "./rows/NoticeRowView";
import { SteeringRowView } from "./rows/SteeringRowView";
import { CompactionRowView } from "./rows/CompactionRowView";
import { ErrorRowView } from "./rows/ErrorRowView";
import { TurnEndRowView } from "./rows/TurnEndRowView";
import { CommandRowView } from "./rows/CommandRowView";

function RowView({ row, streaming }: { row: Row; streaming: boolean }): React.ReactElement {
  switch (row.kind) {
    case "user":
      return <UserRowView row={row} />;
    case "assistant":
      return <AssistantRowView row={row} streaming={streaming} />;
    case "thinking":
      return <ThinkingRowView row={row} />;
    case "tool":
      return <ToolRowView row={row} />;
    case "bash_notice":
      return <NoticeRowView row={row} />;
    case "steering":
      return <SteeringRowView row={row} />;
    case "compaction":
      return <CompactionRowView row={row} />;
    case "error":
      return <ErrorRowView row={row} />;
    case "turn_end":
      return <TurnEndRowView row={row} />;
    case "command":
      return <CommandRowView row={row} />;
    default:
      return <></>;
  }
}

/**
 * The log.
 *
 * A single vertical stream with no bubbles. It follows the tail while the user is
 * already at the bottom, and stops following the moment they scroll up to read —
 * the classic streaming-UI bug is yanking the viewport away from a reader.
 */
export function Transcript({
  rows,
  turnRunning,
}: {
  rows: readonly Row[];
  turnRunning: boolean;
}): React.ReactElement {
  const endRef = React.useRef<HTMLDivElement | null>(null);
  const scrollRef = React.useRef<HTMLDivElement | null>(null);
  const contentRef = React.useRef<HTMLDivElement | null>(null);
  const pinnedRef = React.useRef(true);

  React.useEffect(() => {
    const node = scrollRef.current;
    if (!node) return;
    const onScroll = (): void => {
      const distance = node.scrollHeight - node.scrollTop - node.clientHeight;
      pinnedRef.current = distance < 64;
    };
    node.addEventListener("scroll", onScroll, { passive: true });
    return () => node.removeEventListener("scroll", onScroll);
  }, []);

  // A new row, or a new identity for the last row, is the obvious "follow the tail"
  // trigger.
  React.useEffect(() => {
    if (!pinnedRef.current) return;
    endRef.current?.scrollIntoView({ block: "end" });
  }, [rows.length, rows[rows.length - 1]]);

  /**
   * Follow the tail when an *existing* row changes height.
   *
   * Row count and identity are not enough. A streamed assistant row reflows after
   * its final token, a webfont swap changes the metrics of text already laid out,
   * an inline image decodes at its natural size, and a collapsible opening adds
   * height — none of which add a row. Without this observer the content grows
   * below the fold, and the scroll listener cannot self-correct: it measures the
   * distance at the last scroll event, so it keeps reporting "pinned" while the
   * real distance has grown.
   *
   * Deliberately not `scrollTop = scrollHeight`: that would also fire for a reader
   * scrolled up, which is the behaviour this component exists to avoid.
   */
  React.useEffect(() => {
    const content = contentRef.current;
    if (!content || typeof ResizeObserver === "undefined") return;
    let frame = 0;
    const observer = new ResizeObserver(() => {
      // A single reflow can deliver several callbacks; coalesce them into one
      // scroll at the next paint instead of queueing a scroll per callback.
      if (frame !== 0) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        if (!pinnedRef.current) return;
        endRef.current?.scrollIntoView({ block: "end" });
      });
    });
    observer.observe(content);
    return () => {
      observer.disconnect();
      if (frame !== 0) cancelAnimationFrame(frame);
    };
  }, []);

  return (
    <div
      ref={scrollRef}
      className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-2"
    >
      <div
        ref={contentRef}
        role="log"
        aria-live="polite"
        aria-relevant="additions text"
        aria-label="Session transcript"
        className="mx-auto flex w-full max-w-5xl flex-col gap-1.5"
      >
        {rows.length === 0 ? <EmptyTranscript /> : null}
        {rows.map((row, index) => (
          <RowView
            key={row.id}
            row={row}
            streaming={turnRunning && index === rows.length - 1}
          />
        ))}
        <div ref={endRef} aria-hidden="true" className="h-px" />
      </div>
    </div>
  );
}

function EmptyTranscript(): React.ReactElement {
  return (
    <div className="py-12 text-center">
      <p className="font-mono text-xs text-ink-dim">
        No turns yet. Type a prompt, or <Kbd>/</Kbd> for commands.
      </p>
    </div>
  );
}

function Kbd({ children }: { children: React.ReactNode }): React.ReactElement {
  return (
    <kbd className="rounded-[3px] border border-line bg-elevated px-1 py-px font-mono text-2xs text-ink-muted">
      {children}
    </kbd>
  );
}
