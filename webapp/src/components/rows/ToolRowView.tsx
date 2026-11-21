import * as React from "react";
import { Check, ChevronRight, Loader2, X } from "lucide-react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Badge } from "@/components/ui/badge";
import { RowFrame, type RowTone } from "./RowFrame";
import type { ToolRow } from "@/lib/transcript";

const STATUS_TONE: Record<ToolRow["status"], RowTone> = {
  running: "accent",
  ok: "ok",
  failed: "danger",
};

const STATUS_LABEL: Record<ToolRow["status"], string> = {
  running: "running",
  ok: "ok",
  failed: "failed",
};

function StatusIcon({ status }: { status: ToolRow["status"] }): React.ReactElement {
  if (status === "running") {
    return (
      <Loader2
        aria-hidden="true"
        className="size-3 animate-spin text-accent motion-reduce:animate-none"
      />
    );
  }
  if (status === "ok") return <Check aria-hidden="true" className="size-3 text-ok" />;
  return <X aria-hidden="true" className="size-3 text-danger" />;
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

/** Flatten the free-form `actions` payload into chip labels. */
function actionChips(actions: Record<string, unknown> | null): string[] {
  if (!actions) return [];
  const chips: string[] = [];
  for (const [key, value] of Object.entries(actions)) {
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
      chips.push(`${key}: ${String(value)}`);
    } else if (Array.isArray(value)) {
      chips.push(`${key}: ${value.length}`);
    } else if (value !== null && typeof value === "object") {
      chips.push(key);
    }
  }
  return chips;
}

/**
 * One kernel cell: a collapsing log entry whose header line carries the status
 * and cost, and whose body is the python that ran plus what came back.
 */
export function ToolRowView({ row }: { row: ToolRow }): React.ReactElement {
  const chips = actionChips(row.actions);
  const hasBody = row.code !== "" || row.output !== "" || row.images.length > 0;
  // Failures and images are the point of the row, so open them unprompted.
  const defaultOpen = row.status === "failed" || row.images.length > 0;

  return (
    <RowFrame
      label="tool"
      tone={STATUS_TONE[row.status]}
      meta={
        <span className="inline-flex items-center gap-1.5">
          {row.durationMs !== null ? <span>{formatDuration(row.durationMs)}</span> : null}
          <span className="sr-only">{STATUS_LABEL[row.status]}</span>
          <StatusIcon status={row.status} />
        </span>
      }
    >
      <Collapsible defaultOpen={defaultOpen}>
        <CollapsibleTrigger asChild disabled={!hasBody}>
          <button
            type="button"
            className="group flex w-full items-start gap-1.5 rounded-[var(--radius-cell)] py-0.5 text-left focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent"
            aria-label={`Tool call, ${STATUS_LABEL[row.status]}. Toggle details.`}
          >
            {hasBody ? (
              <ChevronRight
                aria-hidden="true"
                className="mt-0.5 size-3 shrink-0 text-ink-dim transition-transform duration-[var(--duration-feedback)] group-data-[state=open]:rotate-90"
              />
            ) : (
              <span className="size-3 shrink-0" />
            )}
            <code className="min-w-0 flex-1 truncate font-mono text-xs text-ink-muted">
              {firstLine(row.code) || (row.status === "running" ? "…" : "(no code)")}
            </code>
          </button>
        </CollapsibleTrigger>
        <CollapsibleContent>
          <div className="mt-1 space-y-1 pl-[1.125rem]">
            {row.code ? (
              <pre className="max-h-72 overflow-auto whitespace-pre border border-line border-l-2 border-l-line-strong bg-surface px-2 py-1.5 font-mono text-xs leading-relaxed text-ink">
                <code>{row.code}</code>
              </pre>
            ) : null}
            {row.output ? (
              <pre
                className={
                  row.status === "failed"
                    ? "max-h-72 overflow-auto whitespace-pre-wrap border border-danger/40 bg-danger/5 px-2 py-1.5 font-mono text-xs leading-relaxed text-ink"
                    : "max-h-72 overflow-auto whitespace-pre-wrap border border-line bg-surface px-2 py-1.5 font-mono text-xs leading-relaxed text-ink-muted"
                }
              >
                <code>{row.output}</code>
              </pre>
            ) : null}
            {row.images.length > 0 ? (
              <ul className="flex flex-wrap gap-1.5">
                {row.images.map((image, index) => (
                  <li key={`${row.id}-img-${index}`}>
                    <img
                      src={`data:${image.mimeType};base64,${image.data}`}
                      alt={`Tool output image ${index + 1}`}
                      className="max-h-56 rounded-[var(--radius-cell)] border border-line"
                    />
                  </li>
                ))}
              </ul>
            ) : null}
            {chips.length > 0 ? (
              <ul className="flex flex-wrap gap-1" aria-label="Side effects">
                {chips.map((chip) => (
                  <li key={chip}>
                    <Badge>{chip}</Badge>
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        </CollapsibleContent>
      </Collapsible>
    </RowFrame>
  );
}

function firstLine(code: string): string {
  const line = code.split("\n").find((candidate) => candidate.trim() !== "");
  return line ? line.trim() : "";
}
