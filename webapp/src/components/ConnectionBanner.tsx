import * as React from "react";
import { Alert } from "@heroui/react";
import type { ConnectionStatus } from "@/lib/connection";

const DEFAULT_BANNER_DELAY_MS = 1500;

/** Non-blocking: streaming output stays readable while the socket recovers. */
export function ConnectionBanner({
  status,
  delayMs = DEFAULT_BANNER_DELAY_MS,
}: {
  status: ConnectionStatus;
  delayMs?: number;
}): React.ReactElement | null {
  const [visible, setVisible] = React.useState(false);

  React.useEffect(() => {
    if (status === "connected") {
      setVisible(false);
      return;
    }
    if (delayMs <= 0) {
      setVisible(true);
      return;
    }
    const timer = setTimeout(() => setVisible(true), delayMs);
    return () => clearTimeout(timer);
  }, [status, delayMs]);

  if (status === "connected" || !visible) return null;

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
