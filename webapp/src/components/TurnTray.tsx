import * as React from "react";
import { failedToolCount, stepLabel, type Turn, type TurnStep } from "@/lib/transcript";

export interface TurnTrayProps {
  turn: Turn;
  /** Kept for caller compatibility; live progress is text-only. */
  richUi?: boolean;
  onPrompt?: (text: string) => void;
  onFormState?: (id: string, state: Record<string, unknown>) => void;
}

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

/** True when a tool call is still running after an interim assistant message. */
function hasActiveTrailingTool(turn: Turn): boolean {
  const last = turn.steps[turn.steps.length - 1];
  return last?.kind === "tool_run" && last.rows.some((row) => row.status === "running");
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
 * Transient live turn progress indicator.
 *
 * Shows the Living Waveform alongside the current step ("Thinking",
 * "Running read_csv", ...) while the turn is actively working, and disappears
 * completely once the agent starts replying or the turn settles.
 */
export const TurnTray = React.memo(function TurnTray({
  turn,
}: TurnTrayProps): React.ReactElement | null {
  if (!turn.live) return null;
  if (turn.answer !== null && !hasActiveTrailingTool(turn)) return null;

  const current = turn.steps[turn.steps.length - 1];
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
});
