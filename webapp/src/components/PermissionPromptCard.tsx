import * as React from "react";
import { Button } from "@heroui/react";
import { ShieldAlert } from "lucide-react";
import type { PendingPermission, PermissionDecision } from "@/protocol";

export function PermissionPromptCard({
  permission,
  onRespond,
}: {
  permission: PendingPermission;
  onRespond: (requestId: string, decision: PermissionDecision) => void;
}): React.ReactElement {
  const scopeLabel = permission.scope.replace(/_/g, " ");
  return (
    <div
      role="region"
      aria-label="Permission request"
      className="mx-auto mb-2 w-full max-w-3xl px-3 sm:px-4"
    >
      <div className="rounded-xl border border-warning/40 bg-surface-secondary/90 px-3.5 py-2.5 shadow-sm">
        <div className="flex flex-col gap-2.5 sm:flex-row sm:items-start sm:justify-between sm:gap-3">
          <div className="flex min-w-0 flex-1 items-start gap-2.5">
            <ShieldAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warning" />
            <div className="min-w-0 flex-1">
              <div className="text-xs font-semibold text-foreground">
                Permission required to run on host
              </div>
              <code className="mt-1 block break-all rounded bg-surface px-2 py-1 font-mono text-xs text-foreground">
                {permission.command}
              </code>
              {permission.cwd ? (
                <div className="mt-1 truncate text-[11px] text-muted">
                  in {permission.cwd}
                </div>
              ) : null}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-1.5 sm:shrink-0">
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
