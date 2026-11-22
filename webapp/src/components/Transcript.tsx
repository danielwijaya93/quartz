import * as React from "react";
import { ScrollShadow } from "@heroui/react";
import { ArrowDown } from "lucide-react";
import { directoryLabel } from "@/lib/sidebar";
import {
  groupTurns,
  isTurn,
  lastUserRowId,
  sameConversation,
  type Row,
  type Turn,
} from "@/lib/transcript";
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
    case "system_notice":
      // A harness notice is not a message: it is drawn in the application's voice,
      // in the left column, quieter than anything a speaker said.
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
 * Following is a behaviour, not a policy: streaming output never moves a reader
 * who scrolled away, but two things always do, because they are explicit acts -
 * a row the reader just sent, and a switch to another session. When the view is
 * off the tail, `JumpToNewest` is the way back, and it is the only thing on screen
 * that says output arrived below.
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

  /**
   * Pinned-ness as state, not only as a ref.
   *
   * The ref decides whether output is followed; the state decides whether the way
   * back is on screen, and whether it has news to report. Same measurement, two
   * audiences - and no re-render per scroll, because React bails out when the
   * value has not changed.
   */
  const [pinned, setPinned] = React.useState(true);
  /** Output arrived while the reader was away from the tail. */
  const [hasNew, setHasNew] = React.useState(false);
  /** The way back keeps its node while it holds focus; see `showJump`. */
  const [jumpHeld, setJumpHeld] = React.useState(false);
  /** The row list this render is compared against, to spot the explicit acts. */
  const previousRowsRef = React.useRef<readonly Row[]>([]);

  const pinToTail = React.useCallback((): void => {
    pinnedRef.current = true;
    setPinned(true);
    setHasNew(false);
    endRef.current?.scrollIntoView({ block: "end" });
  }, []);

  React.useEffect(() => {
    const node = scrollRef.current;
    if (!node) return;
    const onScroll = (): void => {
      const distance = node.scrollHeight - node.scrollTop - node.clientHeight;
      const atTail = distance < 64;
      pinnedRef.current = atTail;
      setPinned(atTail);
      // At the tail, there is nothing below to be new.
      if (atTail) setHasNew(false);
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
   * Scroll for the two events that are *asked for*, not merely happen.
   *
   * A reader scrolled up to the middle of a long answer is reading; the stream
   * must not drag them back (that rule is above, and stays). But sending a prompt
   * is an act - and today it was an act performed blind: the reply landed some
   * thousands of pixels below a viewport that never moved. Switching sessions is
   * the same kind of act: the log being replaced is not news to follow, it is a
   * different conversation, and it has to open at its newest message rather than
   * wherever the previous one happened to be scrolled to.
   *
   * Both are read from the row stream itself - a `user` row whose id is new, and a
   * list that shares nothing with the last one (identity *and* words, since history
   * ids come from a position that every session shares) - so every path that sends
   * a prompt (composer, palette, an OpenUI form) and every path that loads a
   * session gets the behaviour without a prop threaded through the app.
   */
  React.useEffect(() => {
    const previous = previousRowsRef.current;
    previousRowsRef.current = rows;
    // The first render lands at the tail on its own (see the follow-the-tail
    // effect above), so there is nothing to correct yet.
    if (previous.length === 0) return;

    const newestUser = lastUserRowId(rows);
    const sent = newestUser !== null && !previous.some((row) => row.id === newestUser);
    if (sent || !sameConversation(previous, rows)) {
      pinToTail();
      return;
    }
    // Everything else is the stream talking. A reader who scrolled up asked to stop
    // being dragged, so the most the transcript does is say there is more below.
    if (pinnedRef.current) return;
    setHasNew(true);
  }, [rows, pinToTail]);

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

  // Hidden the moment the reader is back at the tail - but never while it holds
  // focus: removing the node a keyboard user just pressed would drop focus onto
  // `<body>` and send the next Tab to the top of the page.
  const showJump = !pinned || jumpHeld;

  return (
    <>
      {/* A positioning context, not a layout change: the way back floats over the
          log, so its arrival and departure never move the transcript or the
          composer. Everything here mirrors the flex behaviour of the column it
          replaced, so the scroll area is measured exactly as before. */}
      <div className="relative flex min-h-0 flex-1 flex-col">
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
            {rows.length === 0 ? <EmptyTranscript cwd={cwd} onPrompt={onPrompt} /> : null}
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
        {showJump ? (
          <JumpToNewest hasNew={hasNew} onActivate={pinToTail} onFocusChange={setJumpHeld} />
        ) : null}
      </div>
      <TranscriptAnnouncer rows={rows} turnRunning={turnRunning} />
    </>
  );
}

/**
 * The way back to the newest message.
 *
 * The transcript deliberately stops following a reader who scrolled up, and that
 * was all it did: no way back, no sign that anything was arriving below. A reader
 * who scrolled 400px up mid-turn was, for the rest of the turn, reading a page
 * that silently grew out of date.
 *
 * So: a small, floating, keyboard-reachable control with a real name. A dot and a
 * sentence for a screen reader say what a bare scroll button would not - that the
 * reason to press it is new output, not a lost scrollbar. It is not a live region:
 * the transcript's announcements stay single-sourced, and this is an affordance,
 * not an interruption.
 */
function JumpToNewest({
  hasNew,
  onActivate,
  onFocusChange,
}: {
  hasNew: boolean;
  onActivate: () => void;
  onFocusChange: (focused: boolean) => void;
}): React.ReactElement {
  const handleClick = React.useCallback(
    (event: React.MouseEvent<HTMLButtonElement>): void => {
      onActivate();
      // A pointer has no focus ring to lose, so it can watch the control vanish.
      // A keyboard activation keeps its focus, and the caller keeps the node.
      if (event.detail > 0) onFocusChange(false);
    },
    [onActivate, onFocusChange],
  );

  return (
    <button
      type="button"
      data-testid="jump-to-newest"
      onClick={handleClick}
      onFocus={() => onFocusChange(true)}
      onBlur={() => onFocusChange(false)}
      className="absolute bottom-3 left-1/2 flex -translate-x-1/2 items-center gap-1.5 rounded-full border border-border bg-surface-secondary px-3 py-1.5 text-xs font-medium text-surface-secondary-foreground shadow-md transition-colors duration-[var(--duration-feedback)] hover:bg-surface-secondary/80"
    >
      <ArrowDown aria-hidden="true" className="size-3.5 shrink-0" />
      <span>Jump to the newest message</span>
      {hasNew ? (
        <>
          <span aria-hidden="true" className="text-muted">
            ·
          </span>
          <span aria-hidden="true" className="size-1.5 rounded-full bg-accent" />
          <span className="sr-only">New output arrived below.</span>
        </>
      ) : null}
    </button>
  );
}

/**
 * A session with nothing in it yet.
 *
 * This is the whole of the feedback for "start a session": a fresh session has no
 * messages, so it has no log and no row in the sidebar, and without its folder's
 * name here the screen would look exactly as it did before the tap.
 */
const STARTER_PROMPTS = [
  "Summarize this repository and its key files",
  "Show recent git commits and current status",
  "What tests or checks can I run here?",
];

function EmptyTranscript({
  cwd,
  onPrompt,
}: {
  cwd: string;
  onPrompt: (text: string) => void;
}): React.ReactElement {
  const folder = cwd ? directoryLabel(cwd) : "";
  return (
    <div className="flex flex-col items-center gap-4 py-20 text-center">
      <div className="flex size-10 items-center justify-center rounded-2xl border border-border bg-surface-secondary/70 shadow-xs">
        <span aria-hidden="true" className="size-3 rounded-full bg-accent" />
      </div>
      <div className="space-y-1.5">
        <p className="text-base font-semibold tracking-tight text-foreground" title={cwd || undefined}>
          {folder ? `New session in ${folder}` : "No turns yet"}
        </p>
        <p className="text-sm text-muted">
          Send a prompt to start, or press <kbd className="rounded border border-border bg-surface-secondary px-1.5 py-0.5 font-mono text-xs">/</kbd> for commands.
        </p>
      </div>
      {folder ? (
        <div className="mt-2 flex flex-wrap justify-center gap-2">
          {STARTER_PROMPTS.map((suggestion) => (
            <button
              key={suggestion}
              type="button"
              onClick={() => onPrompt(suggestion)}
              className="cursor-pointer rounded-full border border-border bg-surface/80 px-3 py-1.5 text-xs text-muted transition-colors hover:border-accent/40 hover:bg-surface-secondary hover:text-foreground focus-visible:ring-2 focus-visible:ring-focus focus-visible:outline-none"
            >
              {suggestion}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}