import * as React from "react";
import { Button } from "@heroui/react";
import { Check, ChevronDown, ChevronUp, Copy, ShieldAlert } from "lucide-react";
import type { PendingPermission, PermissionDecision } from "@/protocol";
function isLongCommand(command: string): boolean {
  return command.length > 180 || command.split("\n").length > 4;
}

export function PermissionPromptCard({
  permission,
  onRespond,
}: {
  permission: PendingPermission;
  onRespond: (requestId: string, decision: PermissionDecision) => void;
}): React.ReactElement {
  const scopeLabel = permission.scope.replace(/_/g, " ");
  const [expanded, setExpanded] = React.useState(false);
  const [copied, setCopied] = React.useState(false);
  const copyTimerRef = React.useRef<number | null>(null);

  React.useEffect(() => {
    setExpanded(false);
    setCopied(false);
  }, [permission.requestId]);

  React.useEffect(() => {
    return () => {
      if (copyTimerRef.current !== null) {
        window.clearTimeout(copyTimerRef.current);
      }
    };
  }, []);

  const longCommand = isLongCommand(permission.command);

  const handleCopy = React.useCallback(async () => {
    const clipboard = navigator.clipboard;
    if (!clipboard || typeof clipboard.writeText !== "function") return;
    try {
      await clipboard.writeText(permission.command);
      setCopied(true);
      if (copyTimerRef.current !== null) {
        window.clearTimeout(copyTimerRef.current);
      }
      copyTimerRef.current = window.setTimeout(() => {
        setCopied(false);
        copyTimerRef.current = null;
      }, 1800);
    } catch {
      // Clipboard write may be blocked in non-secure contexts; ignore silently.
    }
  }, [permission.command]);

  return (
    <div
      role="region"
      aria-label="Permission request"
      className="mx-auto mb-2 w-full max-w-3xl shrink-0 px-3 sm:px-4"
    >
      <div className="rounded-xl border border-warning/40 bg-surface-secondary px-3.5 py-2.5 shadow-md">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2">
            <ShieldAlert aria-hidden="true" className="size-4 shrink-0 text-warning" />
            <span className="text-xs font-semibold text-foreground">
              Permission required to run on host
            </span>
            <span className="shrink-0 rounded-full border border-warning/30 bg-warning/10 px-2 py-0.5 text-[10px] font-medium text-warning">
              {scopeLabel}
            </span>
          </div>
          {permission.cwd ? (
            <div
              className="min-w-0 max-w-full truncate text-[11px] text-muted"
              title={permission.cwd}
            >
              in {permission.cwd}
            </div>
          ) : null}
        </div>

        <div className="relative mt-2 rounded-lg border border-border/70 bg-surface">
          <div
            data-testid="permission-command-scroll"
            className={`overflow-y-auto ${expanded ? "max-h-[50vh]" : "max-h-36"}`}
          >
            <code className="block whitespace-pre-wrap break-words px-2.5 py-2 pr-9 font-mono text-xs leading-relaxed text-foreground">
              {permission.command}
            </code>
          </div>
          <button
            type="button"
            onClick={() => void handleCopy()}
            aria-label={copied ? "Copied command" : "Copy command"}
            title={copied ? "Copied" : "Copy command"}
            className="absolute right-1.5 top-1.5 inline-flex size-6 cursor-pointer items-center justify-center rounded-md border border-border/60 bg-surface-secondary/90 text-muted transition-colors hover:bg-surface-secondary hover:text-foreground"
          >
            {copied ? (
              <Check aria-hidden="true" className="size-3 text-success" />
            ) : (
              <Copy aria-hidden="true" className="size-3" />
            )}
          </button>
        </div>

        <div className="mt-2.5 flex flex-wrap items-center justify-between gap-2">
          <div>
            {longCommand ? (
              <button
                type="button"
                onClick={() => setExpanded((prev) => !prev)}
                className="inline-flex cursor-pointer items-center gap-1 rounded-md px-1.5 py-1 text-[11px] font-medium text-muted transition-colors hover:bg-surface hover:text-foreground"
              >
                {expanded ? (
                  <>
                    <ChevronUp aria-hidden="true" className="size-3.5" />
                    Collapse command
                  </>
                ) : (
                  <>
                    <ChevronDown aria-hidden="true" className="size-3.5" />
                    Expand command
                  </>
                )}
              </button>
            ) : null}
          </div>

          <div className="flex flex-wrap items-center gap-1.5">
            <Button
              size="sm"
              variant="secondary"
              onPress={() => onRespond(permission.requestId, "allow_once")}
            >
              Allow once
            </Button>
            <Button
              size="sm"
              variant="primary"
              onPress={() => onRespond(permission.requestId, "allow_session")}
            >
              Allow {scopeLabel} for session
            </Button>
            <Button
              size="sm"
              variant="danger-soft"
              onPress={() => onRespond(permission.requestId, "reject")}
            >
              Reject
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
