import * as React from "react";
import { Alert } from "@heroui/react";

export function ComposerAlerts({
  fatalError,
  payloadError,
}: {
  fatalError: string | null;
  payloadError: string | null;
}): React.ReactElement | null {
  if (fatalError !== null) {
    return (
      <Alert status="danger" role="alert">
        <Alert.Indicator />
        <Alert.Content>
          <Alert.Title>Input is blocked</Alert.Title>
          <Alert.Description>
            <span className="text-sm">{fatalError} — start a new session to continue.</span>
          </Alert.Description>
        </Alert.Content>
      </Alert>
    );
  }
  if (payloadError !== null) {
    return (
      <Alert status="danger" role="alert">
        <Alert.Indicator />
        <Alert.Content>
          <Alert.Title>Message too large</Alert.Title>
          <Alert.Description>
            <span className="text-sm">{payloadError}</span>
          </Alert.Description>
        </Alert.Content>
      </Alert>
    );
  }
  return null;
}
