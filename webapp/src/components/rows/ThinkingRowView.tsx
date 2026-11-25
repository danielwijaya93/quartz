import * as React from "react";
import { Disclosure } from "@heroui/react";
import { Brain } from "lucide-react";
import { approxTokens, lineCount, type ThinkingRow } from "@/lib/transcript";

export interface ThinkingRowViewProps {
  row: ThinkingRow;
  /**
   * Expansion, when the caller owns it.
   *
   * The turn tray opens one step at a time, so it has to drive this rather than
   * let the disclosure keep its own state. Left out, the row stays self-managed
   * and collapsed, which is what a thinking block in the reading column wants.
   */
  isExpanded?: boolean;
  onExpandedChange?: (expanded: boolean) => void;
}

/**
 * Thinking is present but quiet: one collapsed line that says how much there is,
 * expandable on demand. It never competes with the answer for attention.
 */
export const ThinkingRowView = React.memo(function ThinkingRowView({
  row,
  isExpanded,
  onExpandedChange,
}: ThinkingRowViewProps): React.ReactElement {
  const lines = lineCount(row.text);
  const tokens = approxTokens(row.text);

  return (
    <Disclosure
      // Passing `undefined` keeps the disclosure uncontrolled.
      isExpanded={isExpanded}
      onExpandedChange={onExpandedChange}
      className="rounded-lg border border-border bg-surface-secondary/60"
    >
      <Disclosure.Heading>
        <Disclosure.Trigger
          className="flex w-full cursor-pointer items-center gap-2 px-3 py-2 text-left text-xs text-muted"
          aria-label={`Thinking, ${lines} lines, about ${tokens} tokens. Expand to read.`}
        >
          <Brain aria-hidden="true" className="size-3.5 shrink-0" />
          <span className="font-medium">Thinking</span>
          <span aria-hidden="true">·</span>
          <span className="tabular-nums">
            {lines} {lines === 1 ? "line" : "lines"} · ~{tokens} tokens
          </span>
          <Disclosure.Indicator className="ml-auto size-3.5" />
        </Disclosure.Trigger>
      </Disclosure.Heading>
      <Disclosure.Content>
        <Disclosure.Body className="px-3 pb-3">
          <pre className="max-h-72 overflow-auto whitespace-pre-wrap font-mono text-xs leading-relaxed text-muted">
            {row.text}
          </pre>
        </Disclosure.Body>
      </Disclosure.Content>
    </Disclosure>
  );
});
