import * as React from "react";
import { createBot, deleteBot, fetchBots, updateBot } from "./botsApi";
import type { BotConfig, BotFormValues } from "@/types/bots";

export interface UseBotsResult {
  bots: BotConfig[];
  loading: boolean;
  reload: () => Promise<void>;
  create: (payload: BotFormValues) => Promise<void>;
  update: (id: string, payload: BotFormValues) => Promise<void>;
  toggle: (id: string, enabled: boolean) => Promise<void>;
  remove: (id: string) => Promise<void>;
}

export function useBots(): UseBotsResult {
  const [bots, setBots] = React.useState<BotConfig[]>([]);
  const [loading, setLoading] = React.useState(true);

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
    const updated = await updateBot(id, { enabled });
    setBots((prev) => prev.map((b) => (b.id === id ? updated : b)));
  }, []);

  const remove = React.useCallback(async (id: string) => {
    await deleteBot(id);
    setBots((prev) => prev.filter((b) => b.id !== id));
  }, []);

  return { bots, loading, reload, create, update, toggle, remove };
}
