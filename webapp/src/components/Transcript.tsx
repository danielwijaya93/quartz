import * as React from "react";
import { ScrollShadow } from "@heroui/react";
import type { Row } from "@/lib/transcript";
import { TranscriptAnnouncer } from "./TranscriptAnnouncer";
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
/**
 * The A2UI renderer (`@a2ui/react` + lit web components) is a large, opt-in chunk:
 * a user who never opens a dashboard never downloads it. It is also the newest
 * dependency in the tree, so it is isolated behind a boundary rather than in the
 * critical path.
 */
const A2uiRowView = React.lazy(() =>
  import("./rows/A2uiRowView").then((module) => ({ default: module.A2uiRowView })),
);

function RowLoading(): React.ReactElement {
  return (
    <p className="text-xs text-muted" role="status">
      Loading dashboard…
    </p>
  );
}

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
    case "a2ui":
      return (
        <React.Suspense fallback={<RowLoading />}>
          <A2uiRowView row={row} />
        </React.Suspense>
      );
    default:
      return <></>;
  }
}

/**
 * The conversation.
 *
 * A single vertical stream of turns and cells, in a comfortable reading column.
 * It follows the tail while the user is already at the bottom, and stops
 * following the moment they scroll up to read - the classic streaming-UI bug is
 * yanking the viewport away from a reader.
 *
 * Note what is *not* here: `aria-live`. The log is a structure, not an
 * announcement; `TranscriptAnnouncer` owns the live region.
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
   * height - none of which add a row. Without this observer the content grows
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
    <>
      <ScrollShadow
        ref={scrollRef}
        orientation="vertical"
        size={32}
        className="min-h-0 flex-1 px-4 py-5"
      >
        <div
          ref={contentRef}
          role="log"
          aria-label="Session transcript"
          className="mx-auto flex w-full max-w-3xl flex-col gap-4"
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
      </ScrollShadow>
      <TranscriptAnnouncer rows={rows} turnRunning={turnRunning} />
    </>
  );
}

function EmptyTranscript(): React.ReactElement {
  return (
    <div className="flex flex-col items-center gap-2 py-24 text-center">
      <p className="text-base font-medium text-foreground">No turns yet</p>
      <p className="text-sm text-muted">
        Type a prompt below, or press <kbd className="font-mono">/</kbd> for commands.
      </p>
    </div>
  );
}
