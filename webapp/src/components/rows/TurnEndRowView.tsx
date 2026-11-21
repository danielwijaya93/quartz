import * as React from "react";
import type { TurnEndRow } from "@/lib/transcript";

function formatElapsed(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

/** The turn footer: cost and outcome, in one quiet mono line. */
export function TurnEndRowView({ row }: { row: TurnEndRow }): React.ReactElement {
  const tone =
    row.status === "ok" ? "text-ink-dim" : row.status === "error" ? "text-danger" : "text-warn";
  return (
    <div className="flex items-baseline gap-2 py-0.5 font-mono text-2xs text-ink-dim tabular-nums">
      <span className={tone}>{row.status}</span>
      <span>·</span>
      <span>{row.tokens.toLocaleString()} tok</span>
      <span>·</span>
      <span>{formatElapsed(row.elapsedMs)}</span>
      {row.error !== undefined ? <span className="text-danger">· {row.error}</span> : null}
    </div>
  );
}
