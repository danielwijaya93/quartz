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

  const load = React.useCallback(async () => {
    const id = ++sequence.current;
    setLoading(true);
    try {
      const next = await fetchSidebar();
      if (id !== sequence.current) return;
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

  const reload = React.useCallback(() => {
    void load();
  }, [load]);

  return {
    data,
    loading,
    error,
    actionError,
    reload,
    hide: React.useCallback((id: string) => run(() => hideSession(id)), [run]),
    restore: React.useCallback((id: string) => run(() => restoreSession(id)), [run]),
    importSession: React.useCallback((id: string) => run(() => importSession(id)), [run]),
    pin: React.useCallback((id: string) => run(() => pinSession(id)), [run]),
    unpin: React.useCallback((id: string) => run(() => unpinSession(id)), [run]),
    rename: React.useCallback(
      (id: string, title: string) => run(() => renameSession(id, title)),
      [run],
    ),
    addWorkspace: React.useCallback((cwd: string) => run(() => addWorkspace(cwd)), [run]),
    removeWorkspace: React.useCallback((cwd: string) => run(() => removeWorkspace(cwd)), [run]),
  };
}
