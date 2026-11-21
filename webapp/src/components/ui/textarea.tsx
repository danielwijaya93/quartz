import * as React from "react";
import { cn } from "@/lib/utils";

export type TextareaProps = React.TextareaHTMLAttributes<HTMLTextAreaElement> & {
  ref?: React.Ref<HTMLTextAreaElement>;
};

/** Bare textarea: the composer owns its own chrome and sizing. */
export function Textarea({ className, ref, ...props }: TextareaProps): React.ReactElement {
  return (
    <textarea
      ref={ref}
      className={cn(
        "w-full resize-none bg-transparent font-mono text-base leading-relaxed text-ink placeholder:text-ink-dim focus-visible:outline-none",
        className,
      )}
      {...props}
    />
  );
}
