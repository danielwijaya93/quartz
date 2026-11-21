import * as React from "react";
import { cn } from "@/lib/utils";

/** A small mono chip. Never carries meaning by colour alone. */
export function Badge({
  className,
  children,
  ...props
}: React.HTMLAttributes<HTMLSpanElement>): React.ReactElement {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-[3px] border border-line px-1.5 py-px font-mono text-2xs leading-4 text-ink-muted",
        className,
      )}
      {...props}
    >
      {children}
    </span>
  );
}
