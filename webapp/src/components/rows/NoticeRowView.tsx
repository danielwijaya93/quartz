import * as React from "react";
import { ChevronRight, Info, Terminal } from "lucide-react";
import { cn } from "@heroui/react";
import type { BashNoticeRow, SystemNoticeRow } from "@/lib/transcript";

/** Either notice the application can put in the stream, as the transcript sees it. */
export type NoticeRow = BashNoticeRow | SystemNoticeRow;

const SILENT_TAGS = new Set(["progress", "tool-images"]);

function summarizeNotice(tag: string, text: string): string {
  if (tag.startsWith("bash-done")) {
    const dur = /duration:([\d.]+s)/.exec(tag) ?? /duration:([\d.]+s)/.exec(text);
    const exit = /exit:(-?\d+)/.exec(tag) ?? /exit:(-?\d+)/.exec(text);
    const parts = ["bash-done"];
    if (dur?.[1]) parts.push(dur[1]);
    if (exit?.[1] && exit[1] !== "0") parts.push(`exit ${exit[1]}`);
    return parts.join(" · ");
  }
  if (tag === "python-state-restored") {
    const count = /Restored (\d+) Python/.exec(text);
    return count ? `python-state-restored · ${count[1]} vars` : "python-state-restored";
  }
  return tag;
}

/** A background job finished. Quiet collapsible line, expandable on click. */
export function BashNoticeRowView({ row }: { row: BashNoticeRow }): React.ReactElement {
  const [open, setOpen] = React.useState(false);
  const first = row.text.split("\n")[0]?.trim() || "Background job";

  return (
    <section aria-label="Background job" className="text-xs text-muted">
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={open}
        className="flex w-full cursor-pointer items-center gap-2 rounded-lg px-2 py-1 text-left transition-colors hover:bg-surface-secondary/40 hover:text-foreground"
      >
        <ChevronRight
          aria-hidden="true"
          className={cn("size-3 shrink-0 transition-transform", open && "rotate-90")}
        />
        <Terminal aria-hidden="true" className="size-3 shrink-0" />
        <span className="truncate font-mono">{first}</span>
      </button>
      {open ? (
        <pre className="mt-1 max-h-60 overflow-auto whitespace-pre-wrap rounded-lg border border-border/50 bg-surface-secondary/30 px-3 py-2 font-mono text-xs leading-relaxed">
          {row.text}
        </pre>
      ) : null}
    </section>
  );
}

/**
 * Internal harness notices (e.g. bash-done, python-state-restored).
 *
 * Purely internal plumbing (`progress`, `tool-images`) is silenced completely;
 * everything else collapses into a single quiet, expandable line so the
 * assistant's answer remains the focal point.
 */
export function SystemNoticeRowView({ row }: { row: SystemNoticeRow }): React.ReactElement | null {
  const [open, setOpen] = React.useState(false);

  if (SILENT_TAGS.has(row.tag)) {
    return null;
  }

  const summary = summarizeNotice(row.tag, row.text);
  const hasBody = row.text.trim() !== "";

  return (
    <section aria-label="System notice" className="text-xs text-muted">
      <button
        type="button"
        disabled={!hasBody}
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={hasBody ? open : undefined}
        className="flex w-full cursor-pointer items-center gap-2 rounded-lg px-2 py-1 text-left transition-colors hover:bg-surface-secondary/40 hover:text-foreground disabled:cursor-default disabled:hover:bg-transparent"
      >
        {hasBody ? (
          <ChevronRight
            aria-hidden="true"
            className={cn("size-3 shrink-0 transition-transform", open && "rotate-90")}
          />
        ) : (
          <Info aria-hidden="true" className="size-3 shrink-0" />
        )}
        <span className="truncate font-mono">{summary}</span>
      </button>
      {open && hasBody ? (
        <p className="mt-1 max-h-60 overflow-auto whitespace-pre-wrap rounded-lg border border-border/50 bg-surface-secondary/30 px-3 py-2 font-mono text-xs leading-relaxed">
          {row.text}
        </p>
      ) : null}
    </section>
  );
}

/** The notice row, whichever system wrote it. */
export function NoticeRowView({ row }: { row: NoticeRow }): React.ReactElement | null {
  return row.kind === "bash_notice" ? (
    <BashNoticeRowView row={row} />
  ) : (
    <SystemNoticeRowView row={row} />
  );
}
