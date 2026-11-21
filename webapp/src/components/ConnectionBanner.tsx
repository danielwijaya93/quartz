import * as React from "react";
import { Alert } from "@heroui/react";
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
    <div className="px-4 py-2 sm:px-6" role="status" aria-live="polite">
      <div className="mx-auto w-full max-w-3xl">
        <Alert status={reconnecting ? "warning" : "danger"}>
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Title>
              {reconnecting ? "Reconnecting to Quartz…" : "Disconnected from Quartz"}
            </Alert.Title>
            <Alert.Description>
              {reconnecting
                ? "Output stays readable while the connection recovers."
                : "Retrying automatically. Nothing you sent has been lost."}
            </Alert.Description>
          </Alert.Content>
        </Alert>
      </div>
    </div>
  );
}
