import * as React from "react";
import { Spinner } from "@heroui/react";
import { ConnectionBanner } from "./ConnectionBanner";
import type { ConnectionStatus } from "@/lib/connection";
import { trayOwnsStatus } from "@/lib/transcript";

/**
 * One line above the composer: the server's coarse status label, or a reconnect
 * banner if the socket is unhappy. Non-blocking in both cases.
 *
 * Deliberately quiet, and deliberately conditional: while a turn runs, the turn
 * tray's header is already the live line - "Thinking", "Running read_csv", with the
 * transcript's one live region - and the server's own label names the same moment
 * in different words ("Thinking... · running"). Drawn together they read as two
 * turns doing two things, so a label the tray already covers is not repeated here.
 * Anything the tray cannot know still shows, unchanged.
 */
export const StatusLine = React.memo(function StatusLine({
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
  if (trayOwnsStatus(label, turnRunning)) return null;
  return (
    <div
      role="status"
      aria-live="polite"
      className="flex shrink-0 items-center gap-2 px-5 py-1.5 text-xs text-muted"
    >
      <Spinner size="sm" color="current" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
});
