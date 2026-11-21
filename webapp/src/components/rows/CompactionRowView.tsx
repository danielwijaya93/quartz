import * as React from "react";
import { Minus } from "lucide-react";
import type { CompactionRow } from "@/lib/transcript";

/**
 * A boundary marker, not a message: context was summarised here, so everything
 * above is no longer verbatim in the model's window.
 */
export function CompactionRowView({ row }: { row: CompactionRow }): React.ReactElement {
  const delta = row.tokensBefore - row.tokensAfter;
  return (
    <section
      aria-label="Context compacted"
      className="flex items-center gap-2 py-1.5 text-2xs text-ink-dim"
    >
      <span className="h-px flex-1 bg-line" />
      <Minus aria-hidden="true" className="size-3" />
      <span className="font-mono uppercase tracking-[0.08em] text-ink-muted">context compacted</span>
      <span className="font-mono tabular-nums">
        {row.messagesBefore}→{row.messagesAfter} msgs · {row.tokensBefore.toLocaleString()}→
        {row.tokensAfter.toLocaleString()} tok · −{delta.toLocaleString()}
      </span>
      <span className="h-px flex-1 bg-line" />
    </section>
  );
}
