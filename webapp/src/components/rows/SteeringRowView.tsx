import * as React from "react";
import { CornerDownRight } from "lucide-react";
import { extractOpenUIUserText } from "@/lib/openui/lang-helpers";
import type { SteeringRow } from "@/lib/transcript";

/**
 * The user interrupted a running turn with guidance.
 *
 * A quoted, left-ruled block: it must never be mistaken for assistant prose, and
 * a blockquote is exactly the shape that says "someone other than the assistant
 * said this".
 */
export function SteeringRowView({ row }: { row: SteeringRow }): React.ReactElement {
  return (
    <figure className="flex items-start gap-2 rounded-r-lg border-l-3 border-accent bg-accent-soft px-3 py-2">
      <CornerDownRight aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-accent" />
      <blockquote className="min-w-0">
        <figcaption className="text-xs font-medium text-accent-soft-foreground">
          You steered the turn
        </figcaption>
        <p className="mt-0.5 whitespace-pre-wrap text-sm text-accent-soft-foreground">
          {extractOpenUIUserText(row.text)}
        </p>
      </blockquote>
    </figure>
  );
}
