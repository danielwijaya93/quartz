import * as React from "react";
import { cn } from "@heroui/react";
import type { RichUiMode } from "@/lib/richUi";

/**
 * Rich UI on/off toggle.
 *
 * Off by default (Markdown). Turning Rich UI on appends the OpenUI Lang system
 * prompt to the session so the model emits interactive charts, tables, and forms.
 */
export function RichUiToggle({
  mode,
  onChange,
}: {
  mode: RichUiMode;
  onChange: (mode: RichUiMode) => void;
}): React.ReactElement {
  const isRich = mode === "rich";

  return (
    <button
      type="button"
      role="switch"
      aria-checked={isRich}
      aria-label="Rich UI"
      title="Draw replies as OpenUI charts, tables and forms. Off by default (Markdown)."
      onClick={() => onChange(isRich ? "markdown" : "rich")}
      className="inline-flex h-7 cursor-pointer items-center gap-1.5 rounded-lg px-1.5 text-xs font-medium text-muted transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
    >
      <span
        aria-hidden="true"
        className={cn(
          "relative inline-flex h-4 w-7 shrink-0 items-center rounded-full border transition-colors duration-150",
          isRich
            ? "border-foreground/30 bg-foreground/85"
            : "border-border bg-surface-secondary",
        )}
      >
        <span
          className={cn(
            "inline-block size-3 rounded-full transition-transform duration-150",
            isRich
              ? "translate-x-3.5 bg-background"
              : "translate-x-0.5 bg-muted",
          )}
        />
      </span>
      <span className="whitespace-nowrap select-none">Rich UI</span>
    </button>
  );
}
