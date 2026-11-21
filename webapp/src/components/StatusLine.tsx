import * as React from "react";
import { Spinner } from "@heroui/react";
import { ConnectionBanner } from "./ConnectionBanner";
import type { ConnectionStatus } from "@/lib/connection";

/**
 * One line above the composer: the server's coarse status label, or a reconnect
 * banner if the socket is unhappy. Non-blocking in both cases.
 */
export function StatusLine({
  label,
  status,
  turnRunning,
}: {
  label: string | null;
  status: ConnectionStatus;
  turnRunning: boolean;
}): React.ReactElement | null {
  if (status !== "connected") {
    return <ConnectionBanner status={status} />;
  }
  if (label === null) return null;
  return (
    <div
      role="status"
      aria-live="polite"
      className="flex shrink-0 items-center gap-2 px-5 py-1.5 text-xs text-muted"
    >
      <Spinner size="sm" color="current" aria-hidden="true" />
      <span>{label}</span>
      {turnRunning ? <span aria-hidden="true">·</span> : null}
      {turnRunning ? <span>running</span> : null}
    </div>
  );
}
