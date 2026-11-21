import * as React from "react";
import { Disclosure, cn } from "@heroui/react";
import { Sparkles, Wrench } from "lucide-react";
import {
  failedToolCount,
  stepLabel,
  toolRunLabel,
  type Turn,
  type TurnStep,
} from "@/lib/transcript";
import { AssistantRowView } from "./rows/AssistantRowView";
import { ThinkingRowView } from "./rows/ThinkingRowView";
import { ToolRowView } from "./rows/ToolRowView";

export interface TurnTrayProps {
  turn: Turn;
  /** Draw a step's openui-lang narration as a surface rather than prose. */
  richUi: boolean;
  onPrompt: (text: string) => void;
  onFormState: (id: string, state: Record<string, unknown>) => void;
}

/**
 * The live region's text: what the turn is doing, in the fewest words.
 *
 * Deliberately coarser than the visible header. A live text step changes with
 * every token, and a live region that repeats the stream is precisely the
 * per-token announcement the transcript is structured to avoid. The failure
 * count rides along, because a collapsed header is no place for a screen reader
 * to be the last to know that something broke.
 */
function statusLabel(step: TurnStep, failed: number): string {
  const label = step.kind === "text" ? "Writing a reply" : stepLabel(step);
  return failed === 0 ? label : `${label}, ${failed} failed`;
}

/**
 * One turn's machinery, folded shut.
 *
 * The answer is the page; this is the drawer the tool calls went into. It is
 * collapsed always - the header carries the progress instead of opening itself -
 * because an auto-expanding drawer moves the answer while it is being read. One
 * line per turn either way: "Thinking", "Running read_csv", or "4 steps · 2
 * failed".
 */
export function TurnTray({
  turn,
  richUi,
  onPrompt,
  onFormState,
}: TurnTrayProps): React.ReactElement {
  const [open, setOpen] = React.useState(false);
  // One step open at a time: the list is a scan of what happened, not an
  // accordion the reader has to tidy up.
  const [openStepId, setOpenStepId] = React.useState<string | null>(null);

  const listRef = React.useRef<HTMLDivElement | null>(null);
  // Whether the list is pinned to its newest step. A reader who scrolled up is
  // asking to stop being dragged.
  const followRef = React.useRef(true);

  const current = turn.steps[turn.steps.length - 1];
  const failed = failedToolCount(turn);
  const settledLabel = `${turn.steps.length} ${turn.steps.length === 1 ? "step" : "steps"}`;
  const label = turn.live && current !== undefined ? stepLabel(current) : settledLabel;

  const handleScroll = React.useCallback((): void => {
    const node = listRef.current;
    if (!node) return;
    followRef.current = node.scrollHeight - node.scrollTop - node.clientHeight < 24;
  }, []);

  const handleExpandedChange = React.useCallback((expanded: boolean): void => {
    setOpen(expanded);
    // A freshly opened body starts at the newest step, not wherever the last
    // visit left the scroll offset.
    followRef.current = true;
  }, []);

  // The list is height-capped, so a live turn's newest step can arrive below the
  // fold. Jumping the scroll rather than animating it keeps the tray from
  // fighting the transcript's own follow-the-tail scroll.
  React.useEffect(() => {
    if (!open || !turn.live || !followRef.current) return;
    const node = listRef.current;
    if (!node) return;
    if (node.scrollHeight - node.scrollTop - node.clientHeight < 1) return;
    node.scrollTop = node.scrollHeight;
  }, [open, turn.live, turn.steps]);

  return (
    <>
      <Disclosure
        isExpanded={open}
        onExpandedChange={handleExpandedChange}
        className="overflow-hidden rounded-xl border border-border bg-surface-secondary/40"
      >
        <Disclosure.Heading>
          {/* The visible text *is* the accessible name: an aria-label would hide
              the failure count from a screen reader, which is the one thing this
              header must never do. `aria-expanded` already says it opens. */}
          <Disclosure.Trigger className="flex w-full cursor-pointer items-center gap-2 px-3 py-2 text-left text-xs text-muted">
            <Sparkles
              aria-hidden="true"
              className={cn(
                "size-3.5 shrink-0",
                turn.live ? "text-accent motion-safe:animate-pulse" : "text-muted",
              )}
            />
            <span className={cn("min-w-0 truncate", turn.live && "text-foreground")}>{label}</span>
            {failed > 0 ? (
              <>
                <span aria-hidden="true" className="shrink-0">
                  ·
                </span>
                <span className="shrink-0 font-medium text-danger">{failed} failed</span>
              </>
            ) : null}
            <span className="sr-only">Toggle turn details</span>
            <Disclosure.Indicator className="ml-auto size-3.5 shrink-0" />
          </Disclosure.Trigger>
        </Disclosure.Heading>
        <Disclosure.Content role="region">
          {open ? (
            <Disclosure.Body className="px-2 pb-2">
              <div
                ref={listRef}
                data-testid="turn-tray-steps"
                onScroll={handleScroll}
                className="flex max-h-80 flex-col gap-1.5 overflow-y-auto"
              >
                {turn.steps.map((step) => (
                  <StepRow
                    key={step.id}
                    step={step}
                    richUi={richUi}
                    // Only the trailing step of a live turn is still streaming;
                    // telling markdown otherwise would re-shape a half-written
                    // fence on every delta.
                    streaming={turn.live && step.id === current?.id}
                    open={openStepId === step.id}
                    onOpenChange={(expanded) => {
                      setOpenStepId(expanded ? step.id : null);
                    }}
                    onPrompt={onPrompt}
                    onFormState={onFormState}
                  />
                ))}
              </div>
            </Disclosure.Body>
          ) : null}
        </Disclosure.Content>
      </Disclosure>
      {turn.live && current !== undefined ? (
        <p
          aria-live="polite"
          aria-atomic="true"
          role="status"
          data-testid="turn-tray-status"
          className="sr-only"
        >
          {statusLabel(current, failed)}
        </p>
      ) : null}
    </>
  );
}

/**
 * One step in the list.
 *
 * Thinking and interim narration are drawn by their usual views, with the
 * speaker label off: the answer supplies the one `Quartz` a turn gets. A folded
 * tool run owns a disclosure of its own, because "used 3 tools" is the fact and
 * the three cards are the detail.
 */
function StepRow({
  step,
  richUi,
  streaming,
  open,
  onOpenChange,
  onPrompt,
  onFormState,
}: {
  step: TurnStep;
  richUi: boolean;
  /** This step is the live turn's newest one. */
  streaming: boolean;
  open: boolean;
  onOpenChange: (expanded: boolean) => void;
  onPrompt: (text: string) => void;
  onFormState: (id: string, state: Record<string, unknown>) => void;
}): React.ReactElement {
  if (step.kind === "text") {
    return (
      <AssistantRowView
        row={step.row}
        streaming={streaming}
        richUi={richUi}
        onPrompt={onPrompt}
        onFormState={onFormState}
        showSpeaker={false}
      />
    );
  }

  if (step.kind === "thinking") {
    // One line per step, so a run of thinking rows is one thought: the stream
    // split it at a boundary, not the model. The tray is a scan of what the turn
    // did, and two stacked thinking disclosures is the clutter it replaces.
    const text = step.rows.map((row) => row.text).join("\n\n");
    return (
      <ThinkingRowView
        row={{ kind: "thinking", id: step.rows[0]!.id, text }}
        isExpanded={open}
        onExpandedChange={onOpenChange}
      />
    );
  }

  const failed = step.rows.filter((row) => row.status === "failed").length;

  return (
    <Disclosure
      isExpanded={open}
      onExpandedChange={onOpenChange}
      className="rounded-lg border border-border bg-background/50"
    >
      <Disclosure.Heading>
        <Disclosure.Trigger className="flex w-full cursor-pointer items-center gap-2 px-3 py-2 text-left text-xs text-muted">
          <Wrench aria-hidden="true" className="size-3.5 shrink-0" />
          <span className="min-w-0 truncate font-medium">{toolRunLabel(step.rows)}</span>
          {failed > 0 ? (
            <>
              <span aria-hidden="true" className="shrink-0">
                ·
              </span>
              <span className="shrink-0 font-medium text-danger">{failed} failed</span>
            </>
          ) : null}
          <span className="sr-only">Toggle this run's calls</span>
          <Disclosure.Indicator className="ml-auto size-3.5 shrink-0" />
        </Disclosure.Trigger>
      </Disclosure.Heading>
      <Disclosure.Content role="region">
        {open ? (
          <Disclosure.Body className="space-y-1.5 p-2">
            {step.rows.map((row) => (
              <ToolRowView key={row.id} row={row} />
            ))}
          </Disclosure.Body>
        ) : null}
      </Disclosure.Content>
    </Disclosure>
  );
}
