import * as React from "react";
import { Button } from "@heroui/react";
import { ArrowLeft } from "lucide-react";

export function CanvasPage({
  ariaLabel,
  onBack,
  headerAction,
  status,
  configPath,
  children,
}: {
  ariaLabel: string;
  onBack: () => void;
  headerAction?: React.ReactNode;
  status?: { ok: boolean; text: string } | null;
  configPath?: string | null;
  children: React.ReactNode;
}): React.ReactElement {
  return (
    <main
      aria-label={ariaLabel}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.stopPropagation();
          onBack();
        }
      }}
      className="min-h-0 flex-1 overflow-y-auto px-4 py-6 sm:px-5"
    >
      <div className="mx-auto w-full max-w-3xl space-y-5">
        <div className="flex items-center justify-between gap-3">
          <Button
            size="sm"
            variant="ghost"
            onPress={onBack}
            className="h-8 gap-1.5 px-2 text-muted hover:text-foreground"
          >
            <ArrowLeft aria-hidden="true" className="size-4" />
            Back to chat
          </Button>
          {headerAction}
        </div>
        {children}
        {status ? (
          <p role="status" className={status.ok ? "text-xs text-success" : "text-xs text-danger"}>
            {status.text}
          </p>
        ) : null}
        {configPath ? <p className="text-xs text-muted">Stored in {configPath}</p> : null}
      </div>
    </main>
  );
}
