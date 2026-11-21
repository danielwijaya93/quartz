import * as React from "react";
import { Markdown } from "../Markdown";
import type { AssistantRow } from "@/lib/transcript";

/** The answer: full reading width, no card, no gutter - just prose. */
export function AssistantRowView({
  row,
  streaming,
}: {
  row: AssistantRow;
  streaming: boolean;
}): React.ReactElement {
  return (
    <article className="flex flex-col gap-1.5">
      <p className="flex items-center gap-1.5 text-xs font-medium text-muted">
        <span aria-hidden="true" className="size-1.5 rounded-full bg-accent" />
        Quartz
      </p>
      <Markdown streaming={streaming}>{row.text}</Markdown>
    </article>
  );
}
