import * as React from "react";
import { AlertTriangle } from "lucide-react";
import type { ErrorRow } from "@/lib/transcript";

export function ErrorRowView({ row }: { row: ErrorRow }): React.ReactElement {
  return (
    <div
      role={row.fatal ? "alert" : undefined}
      className="flex items-start gap-2 border-l-2 border-danger bg-danger/5 py-1 pl-3"
    >
      <AlertTriangle aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-danger" />
      <p className="min-w-0 whitespace-pre-wrap font-mono text-xs text-ink">
        <span className="mr-1.5 text-2xs uppercase tracking-[0.08em] text-danger">
          {row.fatal ? "fatal" : "error"}
        </span>
        {row.message}
      </p>
    </div>
  );
}
