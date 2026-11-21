import * as React from "react";
import { CornerDownRight } from "lucide-react";
import type { SteeringRow } from "@/lib/transcript";

/** The user interrupted a running turn with guidance. A distinct, quiet marker. */
export function SteeringRowView({ row }: { row: SteeringRow }): React.ReactElement {
  return (
    <div className="flex items-start gap-2 border-l-2 border-warn/60 py-0.5 pl-3">
      <CornerDownRight aria-hidden="true" className="mt-0.5 size-3 shrink-0 text-warn" />
      <p className="font-mono text-xs text-ink">
        <span className="mr-1.5 text-2xs uppercase tracking-[0.08em] text-warn">you steered</span>
        {row.text}
      </p>
    </div>
  );
}
