import * as React from "react";
import {
  createBot,
  deleteBot,
  fetchBots,
  restoreDeletedBot,
  runBotNow,
  toggleBot,
  updateBot,
} from "./botsApi";
import type { BotConfig, BotFormValues } from "@/types/bots";

export interface UseBotsResult {
  bots: BotConfig[];
  loading: boolean;
  togglingIds: ReadonlySet<string>;
  runningIds: ReadonlySet<string>;
  toggleError: string | null;
  reload: () => Promise<void>;
  create: (payload: BotFormValues) => Promise<void>;
  update: (id: string, payload: BotFormValues) => Promise<void>;
  toggle: (id: string, enabled: boolean) => Promise<void>;
  runNow: (id: string) => Promise<void>;
  remove: (id: string) => Promise<void>;
  restore: (id: string) => Promise<BotConfig>;
}

export function useBots(): UseBotsResult {
  const [bots, setBots] = React.useState<BotConfig[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [togglingIds, setTogglingIds] = React.useState<Set<string>>(() => new Set());
  const [runningIds, setRunningIds] = React.useState<Set<string>>(() => new Set());
  const [toggleError, setToggleError] = React.useState<string | null>(null);

  const reload = React.useCallback(async () => {
    setLoading(true);
    try {
      const list = await fetchBots();
      setBots(list);
    } catch {
      // Safe fallback: if /api/bots is unmounted or errors on initial load, keep empty list.
      setBots([]);
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    const controller = new AbortController();
    fetchBots(controller.signal)
      .then((list) => {
        if (!controller.signal.aborted) setBots(list);
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

  return {
    bots,
    loading,
    togglingIds,
    runningIds,
    toggleError,
    reload,
    create,
    update,
    toggle,
    runNow,
    remove,
    restore,
  };
}
