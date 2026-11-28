import * as React from "react";
import { cn } from "@heroui/react";
import { formatCostUsd, formatTokenWindow } from "./composerHelpers";

export function ContextRing({
  pct,
  maxTokens,
  cacheHitPct,
  totalCostUsd,
}: {
  pct: number;
  maxTokens?: number;
  cacheHitPct?: number;
  totalCostUsd?: number;
}): React.ReactElement {
  const [open, setOpen] = React.useState(false);
  const containerRef = React.useRef<HTMLDivElement | null>(null);
  const clamped = Math.max(0, Math.min(100, pct));
  const visualPct = clamped > 0 && clamped < 3 ? 3 : clamped;
  const radius = 7;
  const circumference = 2 * Math.PI * radius;
  const dashOffset = circumference * (1 - visualPct / 100);
  const contextText =
    maxTokens && maxTokens > 0
      ? `${pct.toFixed(1)}% of ${formatTokenWindow(maxTokens)}`
      : `${pct.toFixed(1)}%`;
  const cacheText = cacheHitPct !== undefined ? `${cacheHitPct.toFixed(1)}%` : null;
  const costText = totalCostUsd !== undefined ? formatCostUsd(totalCostUsd) : null;
  const labelParts = [`Context: ${contextText}`];
  if (cacheText !== null) labelParts.push(`Cache hit: ${cacheText}`);
  if (costText !== null) labelParts.push(`Total cost: ${costText}`);
  const label = labelParts.join(" · ");
  const toneClass =
    clamped >= 90 ? "text-danger" : clamped >= 75 ? "text-warning" : "text-foreground/75";

  React.useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent): void => {
      if (!containerRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  return (
    <div ref={containerRef} className="relative inline-flex shrink-0 items-center">
      <button
        type="button"
        aria-label={label}
        aria-expanded={open}
        title={label}
        onClick={() => setOpen((prev) => !prev)}
        className={cn(
          "inline-flex size-6 shrink-0 cursor-pointer items-center justify-center rounded-full hover:bg-default/50",
          toneClass,
        )}
      >
        <svg viewBox="0 0 18 18" className="size-4 -rotate-90" aria-hidden="true">
          <circle
            cx="9"
            cy="9"
            r={radius}
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeOpacity={0.18}
          />
          <circle
            cx="9"
            cy="9"
            r={radius}
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeDasharray={circumference}
            strokeDashoffset={dashOffset}
          />
        </svg>
      </button>
      {open ? (
        <div
          role="status"
          className="absolute right-0 bottom-full z-30 mb-2 w-max max-w-[240px] rounded-lg border border-border bg-content1 px-3 py-2 text-xs text-foreground shadow-md"
        >
          <div className="flex items-center justify-between gap-4">
            <span className="text-muted">Context</span>
            <span className="font-medium tabular-nums">{contextText}</span>
          </div>
          {cacheText !== null ? (
            <div className="mt-1 flex items-center justify-between gap-4">
              <span className="text-muted">Cache hit</span>
              <span className="font-medium tabular-nums">{cacheText}</span>
            </div>
          ) : null}
          {costText !== null ? (
            <div className="mt-1 flex items-center justify-between gap-4">
              <span className="text-muted">Total cost</span>
              <span className="font-medium tabular-nums">{costText}</span>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
