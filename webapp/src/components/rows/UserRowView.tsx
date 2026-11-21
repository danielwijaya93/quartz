import * as React from "react";
import { Card } from "@heroui/react";
import type { UserRow } from "@/lib/transcript";

/**
 * The user's side of the conversation.
 *
 * A distinct card, right-aligned against a full-width assistant column: the
 * speaker is legible at a glance without a gutter label doing the work.
 */
export function UserRowView({ row }: { row: UserRow }): React.ReactElement {
  return (
    <article className="flex flex-col items-end gap-1.5 pl-6">
      <p className="text-xs font-medium text-muted">You</p>
      <Card variant="secondary" className="max-w-full px-4 py-3">
        <p className="whitespace-pre-wrap text-[0.9375rem] leading-relaxed text-surface-secondary-foreground">
          {row.text}
        </p>
        {row.images.length > 0 ? (
          <ul className="mt-2 flex flex-wrap gap-2">
            {row.images.map((image, index) => (
              <li key={`${row.id}-img-${index}`}>
                <img
                  src={`data:${image.mimeType};base64,${image.data}`}
                  alt={`Attached image ${index + 1}`}
                  className="max-h-40 rounded-md border border-border"
                />
              </li>
            ))}
          </ul>
        ) : null}
      </Card>
    </article>
  );
}
