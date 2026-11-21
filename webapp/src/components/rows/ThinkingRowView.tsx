import * as React from "react";
import { ChevronRight } from "lucide-react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { RowFrame } from "./RowFrame";
import { approxTokens, lineCount, type ThinkingRow } from "@/lib/transcript";

/**
 * Thinking is present but quiet: collapsed to one dimmed line, expandable on
 * demand. It never competes with the answer for attention.
 */
export function ThinkingRowView({ row }: { row: ThinkingRow }): React.ReactElement {
  const lines = lineCount(row.text);
  const tokens = approxTokens(row.text);

  return (
    <RowFrame label="thinking" tone="muted">
      <Collapsible>
        <CollapsibleTrigger asChild>
          <button
            type="button"
            className="group flex w-full items-center gap-1.5 rounded-[var(--radius-cell)] py-0.5 text-left text-xs text-ink-dim transition-colors duration-[var(--duration-feedback)] hover:text-ink-muted focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent"
            aria-label={`Thinking, ${lines} lines, about ${tokens} tokens. Expand to read.`}
          >
            <ChevronRight
              aria-hidden="true"
              className="size-3 shrink-0 transition-transform duration-[var(--duration-feedback)] group-data-[state=open]:rotate-90"
            />
            <span className="font-mono text-2xs tabular-nums">
              {lines} {lines === 1 ? "line" : "lines"} · ~{tokens} tok
            </span>
          </button>
        </CollapsibleTrigger>
        <CollapsibleContent>
          <pre className="mt-1 max-h-72 overflow-auto whitespace-pre-wrap border-l border-line pl-2 font-mono text-xs leading-relaxed text-ink-dim">
            {row.text}
          </pre>
        </CollapsibleContent>
      </Collapsible>
    </RowFrame>
  );
}
