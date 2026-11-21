import * as React from "react";
import { Alert } from "@heroui/react";
import type { BashNoticeRow } from "@/lib/transcript";

/** A background job finished. System voice, not assistant voice. */
export function NoticeRowView({ row }: { row: BashNoticeRow }): React.ReactElement {
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
