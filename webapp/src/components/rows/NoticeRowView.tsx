import * as React from "react";
import { Terminal } from "lucide-react";
import type { BashNoticeRow } from "@/lib/transcript";

/** A background job finished. System voice, not assistant voice. */
export function NoticeRowView({ row }: { row: BashNoticeRow }): React.ReactElement {
  return (
    <div className="flex items-start gap-2 border-l-2 border-line py-0.5 pl-3 text-xs text-ink-muted">
      <Terminal aria-hidden="true" className="mt-0.5 size-3 shrink-0 text-ink-dim" />
      <span className="sr-only">System notice: </span>
      <p className="whitespace-pre-wrap font-mono text-2xs leading-5">{row.text}</p>
    </div>
  );
}
