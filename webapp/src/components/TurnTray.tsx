import * as React from "react";
import {
  failedToolCount,
  segmentTurn,
  stepLabel,
  type Turn,
  type TurnSegment,
  type TurnStep,
} from "@/lib/transcript";

export interface TurnTrayProps {
  turn: Turn;
  /** Kept for caller compatibility; live progress is text-only. */
  richUi?: boolean;
  onPrompt?: (text: string) => void;
  onFormState?: (id: string, state: Record<string, unknown>) => void;
}

export type WorkSegment = Extract<TurnSegment, { kind: "work" }>;

/**
 * The live region's text: what the turn is doing, in the fewest words.
 *
 * Deliberately coarser than token streaming. The failure count rides along so
 * screen readers still hear if a tool failed during a live turn.
 */
function statusLabel(step: TurnStep | undefined, failed: number): string {
  const label = step === undefined ? "Thinking" : step.kind === "text" ? "Writing a reply" : stepLabel(step);
  return failed === 0 ? label : `${label}, ${failed} failed`;
}

function segmentFailedCount(steps: WorkSegment["steps"]): number {
  let failed = 0;
  for (const step of steps) {
    if (step.kind !== "tool_run") continue;
    for (const row of step.rows) {
      if (row.status === "failed") failed += 1;
    }
  }
  return failed;
}

function LivingWaveform(): React.ReactElement {
  return (
    <span
      data-testid="living-waveform"
      aria-hidden="true"
      className="inline-flex h-3.5 shrink-0 items-center gap-[2.5px]"
    >
      <span className="quartz-waveform-bar" style={{ animationDelay: "0ms" }} />
      <span className="quartz-waveform-bar" style={{ animationDelay: "160ms" }} />
      <span className="quartz-waveform-bar" style={{ animationDelay: "320ms" }} />
      <span className="quartz-waveform-bar" style={{ animationDelay: "480ms" }} />
    </span>
  );
}

/**
 * One contiguous work phase (thinking + tool calls).
 *
 * Shows the Living Waveform alongside the current step while active, and settles
 * in place into a single-sentence summary (e.g. "Ran 2 commands · Edited 3 files")
 * once the phase completes.
 */
export const WorkSegmentRow = React.memo(function WorkSegmentRow({
  segment,
}: {
  segment: WorkSegment;
}): React.ReactElement {
  if (!segment.live) {
    return (
      <div data-testid="work-summary" className="px-1 py-0.5 text-xs text-muted">
        {segment.summary}
      </div>
    );
  }

  const current = segment.steps[segment.steps.length - 1];
  const failed = segmentFailedCount(segment.steps);
  const label = current !== undefined ? stepLabel(current) : "Thinking";

  return (
    <div className="flex items-center gap-2.5 px-1 py-1 text-xs text-muted">
      <LivingWaveform />
      <span aria-hidden="true" className="min-w-0 truncate text-foreground/85">
        {label}
      </span>
      <p
        role="status"
        aria-live="polite"
        aria-atomic="true"
        data-testid="turn-tray-status"
        className="sr-only"
      >
        {statusLabel(current, failed)}
      </p>
    </div>
  );
});

/**
 * Renders the latest work segment of a turn (live waveform while active,
 * or settled summary once complete).
 */
export const TurnTray = React.memo(function TurnTray({
  turn,
}: TurnTrayProps): React.ReactElement | null {
  const workSegments = segmentTurn(turn).filter(
    (seg): seg is WorkSegment => seg.kind === "work",
  );
  const lastWork = workSegments[workSegments.length - 1];
  if (lastWork === undefined) return null;

  if (lastWork.live) {
    const current = lastWork.steps[lastWork.steps.length - 1];
    const failed = failedToolCount(turn);
    const label = current !== undefined ? stepLabel(current) : "Thinking";
    return (
      <div className="flex items-center gap-2.5 px-1 py-1 text-xs text-muted">
        <LivingWaveform />
        <span aria-hidden="true" className="min-w-0 truncate text-foreground/85">
          {label}
        </span>
        <p
          role="status"
          aria-live="polite"
          aria-atomic="true"
          data-testid="turn-tray-status"
          className="sr-only"
        >
          {statusLabel(current, failed)}
        </p>
      </div>
    );
  }

  return <WorkSegmentRow segment={lastWork} />;
});
