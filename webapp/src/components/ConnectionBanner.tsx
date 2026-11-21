import * as React from "react";
import { Loader2, WifiOff } from "lucide-react";
import type { ConnectionStatus } from "@/lib/connection";

/** Non-blocking: streaming output stays readable while the socket recovers. */
export function ConnectionBanner({
  status,
}: {
  status: ConnectionStatus;
}): React.ReactElement | null {
  if (status === "connected") return null;

  const reconnecting = status === "connecting";
  return (
    <div
      role="status"
      aria-live="polite"
      className="flex shrink-0 items-center gap-2 border-t border-warn/40 bg-warn/5 px-4 py-1 font-mono text-2xs text-ink-muted"
    >
      {reconnecting ? (
        <Loader2
          aria-hidden="true"
          className="size-3 animate-spin text-warn motion-reduce:animate-none"
        />
      ) : (
        <WifiOff aria-hidden="true" className="size-3 text-danger" />
      )}
      <span>
        {reconnecting
          ? "Reconnecting to quartz…"
          : "Disconnected from quartz. Retrying automatically."}
      </span>
    </div>
  );
}
