import * as React from "react";
import type { SessionActivity } from "@/lib/sidebar";

export function SessionStatusDot({
  activity,
}: {
  activity?: SessionActivity;
}): React.ReactElement | null {
  const status = activity?.status ?? "idle";
  if (status === "running") {
    return (
      <span
        aria-label="Working"
        title="Working"
        className="mr-1.5 size-1.5 shrink-0 rounded-full bg-blue-500 motion-safe:animate-pulse"
      />
    );
  }
  if (status === "done") {
    return (
      <span
        aria-label="Done"
        title="Done"
        className="mr-1.5 size-1.5 shrink-0 rounded-full bg-emerald-500"
      />
    );
  }
  if (status === "permission" || status === "error") {
    return (
      <span
        aria-label="Needs you"
        title="Needs you"
        className="mr-1.5 size-1.5 shrink-0 rounded-full bg-orange-500"
      />
    );
  }
  return null;
}
