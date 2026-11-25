import * as React from "react";
import {
  addWorkspace,
  fetchSidebar,
  hideSession,
  importSession,
  pinSession,
  removeWorkspace,
  renameSession,
  restoreSession,
  unpinSession,
} from "./sidebarApi";
import type { SidebarPayload } from "./sidebar";

export interface SidebarApi {
  /** The last good payload; null until the first successful load. */
  data: SidebarPayload | null;
  loading: boolean;
  /** A load failure, shown with a retry instead of an empty list. */
  error: string | null;
  /** A mutation failure. The last good data is still on screen. */
  actionError: string | null;
  reload: () => void;
  hide: (sessionId: string) => Promise<boolean>;
  restore: (sessionId: string) => Promise<boolean>;
  importSession: (sessionId: string) => Promise<boolean>;
  pin: (sessionId: string) => Promise<boolean>;
  unpin: (sessionId: string) => Promise<boolean>;
  rename: (sessionId: string, title: string) => Promise<boolean>;
  addWorkspace: (cwd: string) => Promise<boolean>;
  removeWorkspace: (cwd: string) => Promise<boolean>;
}

function messageOf(cause: unknown): string {
  if (cause instanceof Error && cause.message) return cause.message;
  return "Something went wrong";
}

/**
 * Owns the sidebar's server state: one payload, one in-flight load, and the
 * mutations that replace it.
 *
 * Every mutation returns the whole new snapshot from the server, so this hook never
 * patches state locally. That is the property the feature needs: what is on screen
 * and what is in `~/.quartz/web/sidebar.json` cannot disagree, and a reload shows
 * the same page.
 */
export function useSidebar(): SidebarApi {
  const [data, setData] = React.useState<SidebarPayload | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  const [actionError, setActionError] = React.useState<string | null>(null);

  // Monotonic request id: a slow load must never overwrite a newer mutation's
  // snapshot, which is what a plain "ignore the error" guard would do.
  const sequence = React.useRef(0);
  const hasDataRef = React.useRef(false);
  const reloadTimerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = React.useCallback(async () => {
    const id = ++sequence.current;
    // Only show the loading state before the first snapshot arrives so background
    // sidebar refreshes on session switches don't trigger an extra full-app render.
    if (!hasDataRef.current) setLoading(true);
    try {
      const next = await fetchSidebar();
      if (id !== sequence.current) return;
      hasDataRef.current = true;
      setData(next);
      setError(null);
    } catch (cause) {
      if (id !== sequence.current) return;
      setError(messageOf(cause));
    } finally {
      if (id === sequence.current) setLoading(false);
    }
  }, []);

  const run = React.useCallback(
    async (action: () => Promise<SidebarPayload>): Promise<boolean> => {
      setActionError(null);
      try {
        const next = await action();
        sequence.current += 1;
        hasDataRef.current = true;
        setData(next);
        setError(null);
        setLoading(false);
        return true;
      } catch (cause) {
        setActionError(messageOf(cause));
        return false;
      }
    },
    [],
  );

  React.useEffect(() => {
    void load();
  }, [load]);

  // Coalesce bursty reload triggers (e.g. multiple tiles binding or turns finishing
  // simultaneously) into a single network fetch while keeping retry immediate.
  const reload = React.useCallback(() => {
    if (!hasDataRef.current) {
      void load();
      return;
    }
    if (reloadTimerRef.current !== null) return;
    reloadTimerRef.current = setTimeout(() => {
      reloadTimerRef.current = null;
      void load();
    }, 120);
  }, [load]);

  React.useEffect(
    () => () => {
      if (reloadTimerRef.current !== null) {
        clearTimeout(reloadTimerRef.current);
        reloadTimerRef.current = null;
      }
    },
    [],
  );

  const hide = React.useCallback((id: string) => run(() => hideSession(id)), [run]);
  const restore = React.useCallback((id: string) => run(() => restoreSession(id)), [run]);
  const importSessionCb = React.useCallback((id: string) => run(() => importSession(id)), [run]);
  const pin = React.useCallback((id: string) => run(() => pinSession(id)), [run]);
  const unpin = React.useCallback((id: string) => run(() => unpinSession(id)), [run]);
  const rename = React.useCallback(
    (id: string, title: string) => run(() => renameSession(id, title)),
    [run],
  );
  const addWorkspaceCb = React.useCallback((cwd: string) => run(() => addWorkspace(cwd)), [run]);
  const removeWorkspaceCb = React.useCallback(
    (cwd: string) => run(() => removeWorkspace(cwd)),
    [run],
  );

  return React.useMemo(
    () => ({
      data,
      loading,
      error,
      actionError,
      reload,
      hide,
      restore,
      importSession: importSessionCb,
      pin,
      unpin,
      rename,
      addWorkspace: addWorkspaceCb,
      removeWorkspace: removeWorkspaceCb,
    }),
    [
      data,
      loading,
      error,
      actionError,
      reload,
      hide,
      restore,
      importSessionCb,
      pin,
      unpin,
      rename,
      addWorkspaceCb,
      removeWorkspaceCb,
    ],
  );
}
