import * as React from "react";
import { Card, Chip, Disclosure, Spinner } from "@heroui/react";
import { Check, X } from "lucide-react";
import { firstLine, type ToolRow } from "@/lib/transcript";

const STATUS_COPY: Record<
  ToolRow["status"],
  { label: string; color: "default" | "danger" }
> = {
  running: { label: "Running", color: "default" },
  ok: { label: "Succeeded", color: "default" },
  failed: { label: "Failed", color: "danger" },
};

function StatusGlyph({ status }: { status: ToolRow["status"] }): React.ReactElement {
  if (status === "running") return <Spinner size="sm" color="current" aria-hidden="true" />;
  if (status === "ok") return <Check aria-hidden="true" className="size-3" />;
  return <X aria-hidden="true" className="size-3" />;
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
 * One kernel cell: the tool that ran, whether it worked, what it cost, and what
 * came back. Collapsed by default because the answer is the point - except when
 * it failed or produced an image, where the body *is* the information.
 */
export function ToolRowView({ row }: { row: ToolRow }): React.ReactElement {
  const chips = actionChips(row.actions);
  const hasBody = row.code !== "" || row.output !== "" || row.images.length > 0;
  const failed = row.status === "failed";
  const defaultOpen = failed || row.images.length > 0;
  const status = STATUS_COPY[row.status];

  return (
    <Card
      variant="default"
      className={
        failed
          ? "gap-0 overflow-hidden rounded-xl border-danger/40 bg-danger-soft p-0"
          : "gap-0 overflow-hidden rounded-xl p-0"
      }
    >
      <Disclosure defaultExpanded={defaultOpen}>
        <Disclosure.Heading>
          <Disclosure.Trigger
            isDisabled={!hasBody}
            aria-label={`Tool call, ${status.label}. Toggle details.`}
            className="flex w-full cursor-pointer items-center gap-2 px-4 py-2.5 text-left disabled:cursor-default"
          >
            {hasBody ? (
              <Disclosure.Indicator className="size-3.5 shrink-0 text-muted" />
            ) : (
              <span aria-hidden="true" className="size-3.5 shrink-0" />
            )}
            <Chip size="sm" variant="soft" color={status.color}>
              <span className="flex items-center gap-1">
                <StatusGlyph status={row.status} />
                {status.label}
              </span>
            </Chip>
            <code className="min-w-0 flex-1 truncate font-mono text-xs text-muted">
              {firstLine(row.code) || (row.status === "running" ? "…" : "No code")}
            </code>
            {row.durationMs !== null ? (
              <span className="shrink-0 text-xs tabular-nums text-muted">
                {formatDuration(row.durationMs)}
              </span>
            ) : null}
          </Disclosure.Trigger>
        </Disclosure.Heading>
        <Disclosure.Content>
          <Disclosure.Body className="space-y-2 px-4 pb-4">
            {row.code ? (
              <pre className="max-h-80 overflow-auto rounded-md border border-border bg-surface-secondary px-3 py-2 font-mono text-xs leading-relaxed text-surface-secondary-foreground">
                <code>{row.code}</code>
              </pre>
            ) : null}
            {row.output ? (
              <pre
                className={
                  failed
                    ? "max-h-80 overflow-auto whitespace-pre-wrap rounded-md border border-danger/30 bg-background px-3 py-2 font-mono text-xs leading-relaxed text-foreground"
                    : "max-h-80 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-surface-secondary px-3 py-2 font-mono text-xs leading-relaxed text-surface-secondary-foreground"
                }
              >
                <code>{row.output}</code>
              </pre>
            ) : null}
            {row.images.length > 0 ? (
              <ul className="flex flex-wrap gap-2">
                {row.images.map((image, index) => (
                  <li key={`${row.id}-img-${index}`}>
                    <img
                      src={`data:${image.mimeType};base64,${image.data}`}
                      alt={`Tool output image ${index + 1}`}
                      className="max-h-64 rounded-md border border-border"
                    />
                  </li>
                ))}
              </ul>
            ) : null}
            {chips.length > 0 ? (
              <ul className="flex flex-wrap gap-1.5" aria-label="Side effects">
                {chips.map((chip) => (
                  <li key={chip}>
                    <Chip size="sm" variant="tertiary" color="default">
                      {chip}
                    </Chip>
                  </li>
                ))}
              </ul>
            ) : null}
          </Disclosure.Body>
        </Disclosure.Content>
      </Disclosure>
    </Card>
  );
}
