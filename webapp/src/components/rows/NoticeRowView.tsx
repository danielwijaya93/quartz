import * as React from "react";
import { Alert } from "@heroui/react";
import { Info } from "lucide-react";
import type { BashNoticeRow, SystemNoticeRow } from "@/lib/transcript";

/** Either notice the application can put in the stream, as the transcript sees it. */
export type NoticeRow = BashNoticeRow | SystemNoticeRow;

/** A background job finished. System voice, not assistant voice. */
export function BashNoticeRowView({ row }: { row: BashNoticeRow }): React.ReactElement {
  return (
    <Alert status="accent" className="rounded-xl">
      <Alert.Indicator />
      <Alert.Content>
        <Alert.Title>Background job</Alert.Title>
        <Alert.Description>
          <span className="whitespace-pre-wrap font-mono text-xs">{row.text}</span>
        </Alert.Description>
      </Alert.Content>
    </Alert>
  );
}

/**
 * Something the application did - restored a resumed session's kernel, reported a
 * checkpoint - drawn in its own voice.
 *
 * These arrive as `user` messages on the wire (it is the only role the harness can
 * inject), which used to put "[python-state-restored]" in a right-aligned "You"
 * bubble: the reader quoted saying something they never typed. So it is drawn as
 * what it is - left, labelled, and smaller than any answer. Deliberately not a
 * card and not an alert: nothing here needs a decision, and a turn's answer has to
 * stay the loudest thing in the column.
 */
export function SystemNoticeRowView({ row }: { row: SystemNoticeRow }): React.ReactElement {
  return (
    <section
      aria-label="System notice"
      className="flex items-start gap-2 rounded-lg border border-border/60 bg-surface-secondary/30 px-3 py-2 text-xs text-muted"
    >
      <Info aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
      <div className="min-w-0 space-y-1">
        <p className="flex flex-wrap items-baseline gap-x-1.5">
          <span className="font-medium">System notice</span>
          <span aria-hidden="true">·</span>
          <span className="font-mono break-all">{row.tag}</span>
        </p>
        {row.text === "" ? null : (
          <p className="whitespace-pre-wrap leading-relaxed">{row.text}</p>
        )}
      </div>
    </section>
  );
}

/** The notice row, whichever system wrote it. */
export function NoticeRowView({ row }: { row: NoticeRow }): React.ReactElement {
  return row.kind === "bash_notice" ? (
    <BashNoticeRowView row={row} />
  ) : (
    <SystemNoticeRowView row={row} />
  );
}
