import * as React from "react";
import { Alert } from "@heroui/react";
import type { ErrorRow } from "@/lib/transcript";

/** An error the user must see, in the library's danger styling. */
export function ErrorRowView({ row }: { row: ErrorRow }): React.ReactElement {
  return (
    <Alert status="danger" role={row.fatal ? "alert" : "group"} className="rounded-xl">
      <Alert.Indicator />
      <Alert.Content>
        <Alert.Title>{row.fatal ? "Session stopped" : "Something went wrong"}</Alert.Title>
        <Alert.Description>
          <span className="whitespace-pre-wrap text-sm">{row.message}</span>
        </Alert.Description>
      </Alert.Content>
    </Alert>
  );
}
