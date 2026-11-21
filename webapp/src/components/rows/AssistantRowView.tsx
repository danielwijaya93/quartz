import * as React from "react";
import { RowFrame } from "./RowFrame";
import { Markdown } from "../Markdown";
import type { AssistantRow } from "@/lib/transcript";

export function AssistantRowView({
  row,
  streaming,
}: {
  row: AssistantRow;
  streaming: boolean;
}): React.ReactElement {
  return (
    <RowFrame label="quartz" tone="accent">
      <Markdown streaming={streaming}>{row.text}</Markdown>
    </RowFrame>
  );
}
