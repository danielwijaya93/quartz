import * as React from "react";
import { authFetch } from "./authToken";
import { apiUrl } from "./serverUrl";

export interface GlobalJobItem {
  pid: number | string;
  command: string;
  label: string;
  elapsed_s: number;
  sessionId?: string;
  sessionTitle?: string;
}

function withSessionQuery(path: string, sessionId?: string): string {
  if (!sessionId) return apiUrl(path);
  const sep = path.includes("?") ? "&" : "?";
  return apiUrl(`${path}${sep}sessionId=${encodeURIComponent(sessionId)}`);
}

export async function fetchAllJobs(): Promise<GlobalJobItem[]> {
  try {
    const resp = await authFetch(apiUrl("/api/jobs?all=true"));
    if (!resp.ok) return [];
    const data = (await resp.json()) as { jobs?: GlobalJobItem[] };
    return Array.isArray(data.jobs) ? data.jobs : [];
  } catch {
    return [];
  }
}

export function useJobActions(onKilled?: () => void | Promise<void>, sessionId?: string) {
  const [tails, setTails] = React.useState<Record<string, string>>({});
  const [busyPid, setBusyPid] = React.useState<string | null>(null);

  const clearTails = React.useCallback(() => setTails({}), []);

  const toggleTail = React.useCallback(
    async (pid: string): Promise<void> => {
      if (tails[pid] !== undefined) {
        setTails((prev) => {
          const next = { ...prev };
          delete next[pid];
          return next;
        });
        return;
      }
      setBusyPid(pid);
      try {
        const resp = await authFetch(
          withSessionQuery(`/api/jobs/${encodeURIComponent(pid)}/tail`, sessionId),
        );
        if (!resp.ok) {
          setTails((prev) => ({ ...prev, [pid]: "(output tail unavailable)" }));
        } else {
          const data = (await resp.json()) as { tail?: string };
          const text = (data.tail ?? "").trim();
          setTails((prev) => ({ ...prev, [pid]: text || "(no output yet)" }));
        }
      } catch {
        setTails((prev) => ({ ...prev, [pid]: "(failed to load output tail)" }));
      } finally {
        setBusyPid(null);
      }
    },
    [tails, sessionId],
  );

  const killJob = React.useCallback(
    async (pid: string): Promise<void> => {
      setBusyPid(pid);
      try {
        await authFetch(withSessionQuery(`/api/jobs/${encodeURIComponent(pid)}/kill`, sessionId), {
          method: "POST",
        });
        await onKilled?.();
      } finally {
        setBusyPid(null);
      }
    },
    [onKilled, sessionId],
  );

  return { tails, busyPid, clearTails, toggleTail, killJob };
}
