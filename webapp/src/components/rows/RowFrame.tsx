import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * The shared spine of every transcript row.
 *
 * Rows are log entries, not bubbles: a left rule plus a small mono gutter label
 * places each one in the stream. `tone` picks the rule colour and the label
 * colour, always alongside text so state is never colour-only.
 */
export type RowTone = "plain" | "muted" | "accent" | "ok" | "warn" | "danger";

const TONE_RULE: Record<RowTone, string> = {
  plain: "border-line",
  muted: "border-line",
  accent: "border-accent",
  ok: "border-ok",
  warn: "border-warn",
  danger: "border-danger",
};

const TONE_LABEL: Record<RowTone, string> = {
  plain: "text-ink-muted",
  muted: "text-ink-dim",
  accent: "text-accent",
  ok: "text-ok",
  warn: "text-warn",
  danger: "text-danger",
};

export interface RowFrameProps {
  /** Mono gutter label, e.g. "you", "quartz", "tool", "notice". */
  label: string;
  tone?: RowTone;
  /** Right-aligned meta on the label line (duration, tokens). */
  meta?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}

export function RowFrame({
  label,
  tone = "plain",
  meta,
  children,
  className,
}: RowFrameProps): React.ReactElement {
  return (
    <article
      className={cn(
        "group border-l-2 pl-3 transition-colors duration-[var(--duration-feedback)]",
        TONE_RULE[tone],
        className,
      )}
    >
      <header className="flex items-baseline gap-2 py-0.5">
        <span
          className={cn(
            "font-mono text-2xs uppercase tracking-[0.08em]",
            TONE_LABEL[tone],
          )}
        >
          {label}
        </span>
        {meta !== undefined ? (
          <span className="ml-auto font-mono text-2xs text-ink-dim tabular-nums">{meta}</span>
        ) : null}
      </header>
      <div className="pb-1.5">{children}</div>
    </article>
  );
}
