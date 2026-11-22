import * as React from "react";
import { ScrollShadow } from "@heroui/react";
import { directoryLabel } from "@/lib/sidebar";
import { groupTurns, isTurn, type Row, type Turn } from "@/lib/transcript";
import { TranscriptAnnouncer } from "./TranscriptAnnouncer";
import { TurnTray } from "./TurnTray";
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

interface RowProps {
  row: Row;
  streaming: boolean;
  richUi: boolean;
  onPrompt: (text: string) => void;
  onFormState: (id: string, state: Record<string, unknown>) => void;
}

function RowView({
  row,
  streaming,
  richUi,
  onPrompt,
  onFormState,
}: RowProps): React.ReactElement {
  switch (row.kind) {
    case "user":
      return <UserRowView row={row} />;
    case "assistant":
      return (
        <AssistantRowView
          row={row}
          streaming={streaming}
          richUi={richUi}
          onPrompt={onPrompt}
          onFormState={onFormState}
        />
      );
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
 * One turn: its machinery folded into a tray, then the answer at full width.
 *
 * The order is the whole point. The work a turn did is something the reader may
 * want to audit and never has to read, so it goes above the answer, one line
 * tall, closed.
 */
function TurnView({
  turn,
  streaming,
  richUi,
  onPrompt,
  onFormState,
}: {
  turn: Turn;
  /** The answer row is the one deltas are still landing in. */
  streaming: boolean;
  richUi: boolean;
  onPrompt: (text: string) => void;
  onFormState: (id: string, state: Record<string, unknown>) => void;
}): React.ReactElement {
  return (
    <div className="flex flex-col gap-2.5">
      {turn.steps.length > 0 ? (
        <TurnTray turn={turn} richUi={richUi} onPrompt={onPrompt} onFormState={onFormState} />
      ) : null}
      {turn.answer !== null ? (
        <AssistantRowView
          row={turn.answer}
          streaming={streaming}
          richUi={richUi}
          onPrompt={onPrompt}
          onFormState={onFormState}
        />
      ) : null}
    </div>
  );
}

/**
 * The conversation.
 *
 * A single vertical stream of turns and cells, in a comfortable reading column.
 * It follows the tail while the user is already at the bottom, and stops
 * following the moment they scroll up to read - the classic streaming-UI bug is
 * yanking the viewport away from a reader.
 *
 * Rows are not rendered one card per event. `groupTurns` folds each turn's
 * thinking and tool calls into a collapsed tray and lifts the answer out of it,
 * so a three-tool turn is two things on screen instead of seven.
 *
 * Note what is *not* here: `aria-live`. The log is a structure, not an
 * announcement; `TranscriptAnnouncer` owns the coarse live region, and a live
 * turn's tray header owns "what is happening now".
 */
export function Transcript({
  rows,
  turnRunning,
  richUi,
  cwd,
  onPrompt,
  onFormState,
}: {
  rows: readonly Row[];
  turnRunning: boolean;
  /** Where the session runs: the empty state says so instead of saying nothing. */
  cwd: string;
  /** Draw openui-lang replies as a live surface rather than prose. */
  richUi: boolean;
  /** Send a message into the session; an OpenUI form submit goes through here. */
  onPrompt: (text: string) => void;
  /** Persist surface form state onto its row so a re-render cannot reset it. */
  onFormState: (id: string, state: Record<string, unknown>) => void;
}): React.ReactElement {
  const items = React.useMemo(() => groupTurns(rows, { running: turnRunning }), [rows, turnRunning]);
  const lastRowId = rows[rows.length - 1]?.id;

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
          {rows.length === 0 ? <EmptyTranscript cwd={cwd} /> : null}
          {items.map((item) =>
            isTurn(item) ? (
              <TurnView
                key={item.id}
                turn={item}
                // Deltas are landing in this row iff it is the last row and the
                // turn still runs; anything else is settled text.
                streaming={item.live && item.answer !== null && item.answer.id === lastRowId}
                richUi={richUi}
                onPrompt={onPrompt}
                onFormState={onFormState}
              />
            ) : (
              <RowView
                key={item.id}
                row={item}
                // Assistant rows never reach here - every one of them belongs to a
                // turn - so the streaming flag is moot for the rows that do.
                streaming={false}
                richUi={richUi}
                onPrompt={onPrompt}
                onFormState={onFormState}
              />
            ),
          )}
          <div ref={endRef} aria-hidden="true" className="h-px" />
        </div>
      </ScrollShadow>
      <TranscriptAnnouncer rows={rows} turnRunning={turnRunning} />
    </>
  );
}

/**
 * A session with nothing in it yet.
 *
 * This is the whole of the feedback for "start a session": a fresh session has no
 * messages, so it has no log and no row in the sidebar, and without its folder's
 * name here the screen would look exactly as it did before the tap.
 */
function EmptyTranscript({ cwd }: { cwd: string }): React.ReactElement {
  const folder = cwd ? directoryLabel(cwd) : "";
  return (
    <div className="flex flex-col items-center gap-2 py-24 text-center">
      <p className="text-base font-medium text-foreground" title={cwd || undefined}>
        {folder ? `New session in ${folder}` : "No turns yet"}
      </p>
      <p className="text-sm text-muted">
        Send a prompt to start, or press <kbd className="font-mono">/</kbd> for commands.
      </p>
    </div>
  );
}