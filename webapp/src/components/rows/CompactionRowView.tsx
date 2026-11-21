import * as React from "react";
import { Separator } from "@heroui/react";
import type { CompactionRow } from "@/lib/transcript";

/**
 * A boundary marker, not a message: context was summarised here, so everything
 * above is no longer verbatim in the model's window.
 */
export function CompactionRowView({ row }: { row: CompactionRow }): React.ReactElement {
  const saved = row.tokensBefore - row.tokensAfter;
  return (
    <section
      aria-label="Context compacted"
      className="flex items-center gap-3 py-1 text-xs text-muted"
    >
      <Separator className="flex-1" />
      <p className="shrink-0 text-center">
        Context compacted · {row.messagesBefore}→{row.messagesAfter} messages ·{" "}
        <span className="tabular-nums">
          {row.tokensBefore.toLocaleString()}→{row.tokensAfter.toLocaleString()} tokens
        </span>{" "}
        · −<span className="tabular-nums">{saved.toLocaleString()}</span>
      </p>
      <Separator className="flex-1" />
    </section>
  );
}
