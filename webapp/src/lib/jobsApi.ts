import * as React from "react";
import { authFetch } from "./authToken";
import { apiUrl } from "./serverUrl";

export function useJobActions(onKilled?: () => void | Promise<void>) {
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
        const resp = await authFetch(apiUrl(`/api/jobs/${encodeURIComponent(pid)}/tail`));
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
    [tails],
  );

  const killJob = React.useCallback(
    async (pid: string): Promise<void> => {
      setBusyPid(pid);
      try {
        await authFetch(apiUrl(`/api/jobs/${encodeURIComponent(pid)}/kill`), { method: "POST" });
        await onKilled?.();
      } finally {
        setBusyPid(null);
      }
    },
    [onKilled],
  );

  return { tails, busyPid, clearTails, toggleTail, killJob };
}
