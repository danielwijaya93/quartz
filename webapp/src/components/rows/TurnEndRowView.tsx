import * as React from "react";
import { Chip } from "@heroui/react";
import type { TurnEndRow } from "@/lib/transcript";

function formatElapsed(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

const STATUS_COPY: Record<
  TurnEndRow["status"],
  { label: string; color: "success" | "warning" | "danger" }
> = {
  ok: { label: "Finished", color: "success" },
  aborted: { label: "Aborted", color: "warning" },
  interrupted: { label: "Interrupted", color: "warning" },
  waiting_permission: { label: "Waiting for permission", color: "warning" },
  error: { label: "Failed", color: "danger" },
};

/** The turn footer: outcome and cost, quiet enough to skip while scanning. */
export function TurnEndRowView({ row }: { row: TurnEndRow }): React.ReactElement {
  const status = STATUS_COPY[row.status];
  return (
    <p className="flex flex-wrap items-center gap-2 text-xs text-muted">
      <Chip size="sm" variant="soft" color={status.color}>
        {status.label}
      </Chip>
      <span className="tabular-nums">{row.tokens.toLocaleString()} tokens</span>
      <span aria-hidden="true">·</span>
      <span className="tabular-nums">{formatElapsed(row.elapsedMs)}</span>
      {row.error !== undefined ? <span className="text-danger">{row.error}</span> : null}
    </p>
  );
}
