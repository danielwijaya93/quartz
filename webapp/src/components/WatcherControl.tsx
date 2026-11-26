import * as React from "react";
import { createPortal } from "react-dom";
import { Button } from "@heroui/react";
import { Eye, SlidersHorizontal, X } from "lucide-react";
import type { WatcherAction, WatcherMode, WatcherState } from "../protocol";

export interface WatcherConfigUpdate {
  mode?: WatcherMode;
  goal?: string;
  promptTemplate?: string;
  delaySeconds?: number;
  maxTurns?: number;
}

export interface WatcherControlProps {
  watcher: WatcherState;
  onConfigure: (config: WatcherConfigUpdate) => void;
  onAction: (action: WatcherAction) => void;
  onOpenSettings?: () => void;
}

const MODE_LABELS: Record<WatcherMode, string> = {
  auto: "Auto",
  goal: "Goal",
  loop: "Loop",
  off: "Off",
};

export function WatcherControl({
  watcher,
  onConfigure,
  onAction,
  onOpenSettings,
}: WatcherControlProps): React.ReactElement {
  const [open, setOpen] = React.useState(false);
  const [modeDraft, setModeDraft] = React.useState<WatcherMode>(watcher.mode);
  const [goalDraft, setGoalDraft] = React.useState(watcher.goal);
  const [promptDraft, setPromptDraft] = React.useState(watcher.promptTemplate);
  const [delayDraft, setDelayDraft] = React.useState(String(watcher.delaySeconds));
  const [maxTurnsDraft, setMaxTurnsDraft] = React.useState(String(watcher.maxTurns));
  const containerRef = React.useRef<HTMLDivElement>(null);
  const dialogRef = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => {
    if (open) return;
    setModeDraft(watcher.mode);
    setGoalDraft(watcher.goal);
    setPromptDraft(watcher.promptTemplate);
    setDelayDraft(String(watcher.delaySeconds));
    setMaxTurnsDraft(String(watcher.maxTurns));
  }, [watcher, open]);

  React.useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    const onPointerDown = (e: PointerEvent) => {
      const target = e.target as Node;
      if (
        !containerRef.current?.contains(target) &&
        !dialogRef.current?.contains(target)
      ) {
        setOpen(false);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("pointerdown", onPointerDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("pointerdown", onPointerDown);
    };
  }, [open]);

  const pillSummary =
    watcher.mode === "goal"
      ? `Goal ${watcher.turnCount}/${watcher.maxTurns}`
      : watcher.mode === "loop"
        ? `Loop ${watcher.turnCount}/${watcher.maxTurns}`
        : !watcher.configured
          ? "Setup"
          : MODE_LABELS[watcher.mode];

  const savePopover = () => {
    const delay = Number.parseInt(delayDraft, 10);
    const maxTurns = Number.parseInt(maxTurnsDraft, 10);
    onConfigure({
      mode: modeDraft,
      goal: goalDraft,
      promptTemplate: promptDraft,
      ...(Number.isFinite(delay) && delay >= 0 && delay <= 3600 ? { delaySeconds: delay } : {}),
      ...(Number.isFinite(maxTurns) && maxTurns >= 1 && maxTurns <= 100 ? { maxTurns } : {}),
    });
    setOpen(false);
  };

  return (
    <div ref={containerRef} className="relative flex shrink-0 items-center">
      <button
        type="button"
        aria-label="Turn Watcher controls"
        aria-expanded={open}
        title={watcher.goal ? `Goal: ${watcher.goal}` : `Turn Watcher: ${pillSummary}`}
        onClick={() => setOpen((v) => !v)}
        className="inline-flex h-7 items-center gap-1.5 rounded-full border border-border bg-surface-secondary/60 px-2.5 text-[11px] font-medium text-foreground/85 transition-colors hover:border-accent/50 hover:bg-surface-secondary focus-visible:ring-2 focus-visible:ring-focus focus-visible:outline-none"
      >
        <Eye aria-hidden="true" className="size-3 shrink-0 text-muted" />
        <span>{pillSummary}</span>
        <SlidersHorizontal aria-hidden="true" className="hidden size-2.5 opacity-70 sm:inline" />
      </button>

      {open
        ? createPortal(
            <div
              ref={dialogRef}
              role="dialog"
              aria-label="Session Turn Watcher Settings"
              className="fixed inset-x-2 top-[calc(3.25rem+env(safe-area-inset-top))] z-50 rounded-xl border border-border bg-surface p-3 shadow-lg sm:left-auto sm:right-4 sm:w-96"
            >
          <div className="mb-2 flex items-center justify-between">
            <span className="text-xs font-semibold text-foreground">Session Turn Watcher</span>
            <button
              type="button"
              aria-label="Close watcher panel"
              onClick={() => setOpen(false)}
              className="rounded p-0.5 text-muted hover:text-foreground"
            >
              <X className="size-3.5" />
            </button>
          </div>

          {!watcher.configured ? (
            <div className="mb-2.5 flex items-center justify-between gap-2 rounded-lg border border-warning/40 bg-warning/10 px-2.5 py-1.5 text-xs text-foreground">
              <span>Inactive until a global watcher model is set in Settings → Advanced.</span>
              {onOpenSettings ? (
                <button
                  type="button"
                  onClick={() => {
                    setOpen(false);
                    onOpenSettings();
                  }}
                  className="shrink-0 font-medium text-accent underline"
                >
                  Open Settings
                </button>
              ) : null}
            </div>
          ) : null}

          <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            <label className="flex flex-col gap-1 text-[11px] text-muted">
              Mode
              <select
                aria-label="Session watcher mode"
                value={modeDraft}
                onChange={(e) => setModeDraft(e.target.value as WatcherMode)}
                className="h-8 rounded-lg border border-border bg-surface px-2 text-xs text-foreground"
              >
                <option value="auto">Auto (anti-stall)</option>
                <option value="goal">Goal (/goal)</option>
                <option value="loop">Turn Loop</option>
                <option value="off">Off</option>
              </select>
            </label>
            <label className="flex flex-col gap-1 text-[11px] text-muted">
              Delay (sec)
              <input
                aria-label="Session watcher delay"
                type="number"
                min={0}
                max={3600}
                value={delayDraft}
                onChange={(e) => setDelayDraft(e.target.value)}
                className="h-8 rounded-lg border border-border bg-surface px-2 font-mono text-xs text-foreground"
              />
            </label>
            <label className="flex flex-col gap-1 text-[11px] text-muted">
              Max turns
              <input
                aria-label="Session watcher max turns"
                type="number"
                min={1}
                max={100}
                value={maxTurnsDraft}
                onChange={(e) => setMaxTurnsDraft(e.target.value)}
                className="h-8 rounded-lg border border-border bg-surface px-2 font-mono text-xs text-foreground"
              />
            </label>
          </div>

          {modeDraft === "goal" ? (
            <label className="mt-2 flex flex-col gap-1 text-[11px] text-muted">
              Goal
              <input
                aria-label="Session goal"
                type="text"
                value={goalDraft}
                placeholder="e.g. Make all tests pass and reduce latency below 120ms"
                onChange={(e) => setGoalDraft(e.target.value)}
                className="h-8 rounded-lg border border-border bg-surface px-2.5 text-xs text-foreground"
              />
            </label>
          ) : null}

          <label className="mt-2 flex flex-col gap-1 text-[11px] text-muted">
            Injected prompt template ({"{turn}"}, {"{max_turns}"}, {"{remaining}"}, {"{goal}"})
            <textarea
              aria-label="Session watcher prompt template"
              rows={2}
              value={promptDraft}
              onChange={(e) => setPromptDraft(e.target.value)}
              className="rounded-lg border border-border bg-surface px-2.5 py-1.5 font-mono text-xs text-foreground"
            />
          </label>

          <div className="mt-2.5 flex items-center justify-end gap-2">
            {watcher.goal ? (
              <Button
                size="sm"
                variant="outline"
                onPress={() => {
                  onAction("clear");
                  setOpen(false);
                }}
              >
                Clear Goal
              </Button>
            ) : null}
            <Button size="sm" variant="primary" onPress={savePopover}>
              Apply
            </Button>
          </div>
        </div>,
            document.body,
          )
        : null}
    </div>
  );
}
