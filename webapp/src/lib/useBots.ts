import * as React from "react";
import {
  createBot,
  deleteBot,
  fetchBots,
  fetchBotsHistory,
  restoreDeletedBot,
  runBotNow,
  toggleBot,
  updateBot,
} from "./botsApi";
import type { BotConfig, BotFormValues, BotRunRecord } from "@/types/bots";

export interface UseBotsResult {
  bots: BotConfig[];
  runs: BotRunRecord[];
  loading: boolean;
  togglingIds: ReadonlySet<string>;
  runningIds: ReadonlySet<string>;
  toggleError: string | null;
  reload: () => Promise<void>;
  reloadRuns: () => Promise<void>;
  create: (payload: BotFormValues) => Promise<void>;
  update: (id: string, payload: BotFormValues) => Promise<void>;
  toggle: (id: string, enabled: boolean) => Promise<void>;
  runNow: (id: string) => Promise<void>;
  remove: (id: string) => Promise<void>;
  restore: (id: string) => Promise<BotConfig>;
}

export function useBots(): UseBotsResult {
  const [bots, setBots] = React.useState<BotConfig[]>([]);
  const [runs, setRuns] = React.useState<BotRunRecord[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [togglingIds, setTogglingIds] = React.useState<Set<string>>(() => new Set());
  const [runningIds, setRunningIds] = React.useState<Set<string>>(() => new Set());
  const [toggleError, setToggleError] = React.useState<string | null>(null);

  const reloadRuns = React.useCallback(async () => {
    try {
      const data = await fetchBotsHistory({ limit: 100 });
      setRuns((prev) => {
        if (
          prev.length === data.runs.length &&
          prev.every(
            (r, i) =>
              r.run_id === data.runs[i]?.run_id &&
              r.status === data.runs[i]?.status &&
              r.finished_at === data.runs[i]?.finished_at,
          )
        ) {
          return prev;
        }
        return data.runs;
      });
    } catch {
      // Safe fallback if /api/bots/history is unavailable.
    }
  }, []);

  const reload = React.useCallback(async () => {
    setLoading(true);
    try {
      const [list, history] = await Promise.all([
        fetchBots(),
        fetchBotsHistory({ limit: 100 }).catch(() => null),
      ]);
      setBots(list);
      if (history) setRuns(history.runs);
    } catch {
      // Safe fallback: if /api/bots is unmounted or errors on initial load, keep empty list.
      setBots([]);
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      fetchBots(controller.signal),
      fetchBotsHistory({ limit: 100, signal: controller.signal }).catch(() => null),
    ])
      .then(([list, history]) => {
        if (controller.signal.aborted) return;
        setBots(list);
        if (history) setRuns(history.runs);
      })
      .catch(() => {
        if (!controller.signal.aborted) setBots([]);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, []);

  const create = React.useCallback(async (payload: BotFormValues) => {
    const created = await createBot(payload);
    setBots((prev) => [...prev, created]);
  }, []);

  const update = React.useCallback(async (id: string, payload: BotFormValues) => {
    const updated = await updateBot(id, payload);
    setBots((prev) => prev.map((b) => (b.id === id ? updated : b)));
  }, []);

  const toggle = React.useCallback(async (id: string, enabled: boolean) => {
    setToggleError(null);
    setTogglingIds((prev) => {
      const next = new Set(prev);
      next.add(id);
      return next;
    });
    try {
      const updated = await toggleBot(id, enabled);
      setBots((prev) => prev.map((b) => (b.id === id ? updated : b)));
    } catch (err) {
      setToggleError(err instanceof Error ? err.message : "Failed to toggle bot.");
    } finally {
      setTogglingIds((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    }
  }, []);

  const runNow = React.useCallback(async (id: string) => {
    setToggleError(null);
    setRunningIds((prev) => {
      const next = new Set(prev);
      next.add(id);
      return next;
    });
    try {
      await runBotNow(id);
    } catch (err) {
      setToggleError(err instanceof Error ? err.message : "Failed to run bot.");
    } finally {
      setRunningIds((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    }
  }, []);

  const remove = React.useCallback(async (id: string) => {
    await deleteBot(id);
    setBots((prev) => prev.filter((b) => b.id !== id));
  }, []);

  const restore = React.useCallback(async (id: string) => {
    const restored = await restoreDeletedBot(id);
    setBots((prev) => [...prev, restored]);
    return restored;
  }, []);

  return React.useMemo(
    () => ({
      bots,
      runs,
      loading,
      togglingIds,
      runningIds,
      toggleError,
      reload,
      reloadRuns,
      create,
      update,
      toggle,
      runNow,
      remove,
      restore,
    }),
    [
      bots,
      runs,
      loading,
      togglingIds,
      runningIds,
      toggleError,
      reload,
      reloadRuns,
      create,
      update,
      toggle,
      runNow,
      remove,
      restore,
    ],
  );
}
