import * as React from "react";
import { RowFrame } from "./RowFrame";
import type { UserRow } from "@/lib/transcript";

export function UserRowView({ row }: { row: UserRow }): React.ReactElement {
  return (
    <RowFrame label="you" tone="muted">
      <p className="whitespace-pre-wrap font-mono text-sm text-ink">{row.text}</p>
      {row.images.length > 0 ? (
        <ul className="mt-1 flex flex-wrap gap-1.5">
          {row.images.map((image, index) => (
            <li key={`${row.id}-img-${index}`}>
              <img
                src={`data:${image.mimeType};base64,${image.data}`}
                alt={`Attached image ${index + 1}`}
                className="max-h-32 rounded-[var(--radius-cell)] border border-line"
              />
            </li>
          ))}
        </ul>
      ) : null}
    </RowFrame>
  );
}
