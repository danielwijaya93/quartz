import * as React from "react";
import { createSurfaceSet, type A2uiSurfaceSet } from "./processor";

/**
 * A processor per frame, and no more than one.
 *
 * `messages` is a row field, so it is the same array for every re-render of the
 * same row. Keying the processor on that identity is what makes rendering
 * idempotent: a state update elsewhere in the transcript re-renders the row and
 * reuses the surface it already built, instead of stacking a duplicate.
 *
 * The other half is release. A processor owns subscriptions (its surface group's
 * listeners, its RPC channel), so a row that unmounts must dispose it or a long
 * session leaks one per surface.
 *
 * Disposal is deferred by a microtask for one specific reason: React's
 * StrictMode mounts every effect twice in development (setup, teardown, setup
 * again) inside a single commit. Disposing synchronously in that teardown would
 * hand the re-mounted component a processor that has already been shut down.
 * The flag is re-set by the second setup before the queued microtask runs, so a
 * StrictMode round-trip keeps the processor and a real unmount releases it.
 */
export function useA2uiSurfaceSet(messages: readonly unknown[]): A2uiSurfaceSet {
  const surfaceSet = React.useMemo(() => createSurfaceSet(messages), [messages]);
  const live = React.useRef(true);

  React.useEffect(() => {
    live.current = true;
    const set = surfaceSet;
    return () => {
      live.current = false;
      void Promise.resolve().then(() => {
        if (!live.current) set.dispose();
      });
    };
  }, [surfaceSet]);

  return surfaceSet;
}
