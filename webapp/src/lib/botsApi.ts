import { authFetch } from "./authToken";
import { apiUrl } from "./serverUrl";
import type { BotConfig, BotFormValues } from "@/types/bots";

const JSON_HEADERS = { "Content-Type": "application/json" } as const;

export class BotsApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "BotsApiError";
    this.status = status;
  }
}

async function readErrorMessage(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (typeof body === "object" && body !== null) {
      const msg = (body as { error?: unknown; detail?: unknown }).error ??
        (body as { detail?: unknown }).detail;
      if (typeof msg === "string" && msg !== "") return msg;
    }
  } catch {
    // Non-JSON error body; fall back to HTTP status.
  }
  return `${response.status} ${response.statusText}`.trim();
}

export async function fetchBots(signal?: AbortSignal): Promise<BotConfig[]> {
  let response: Response;
  try {
    response = await authFetch(apiUrl("/api/bots"), signal ? { signal } : undefined);
  } catch {
    // Network/transport failure when bots route is unavailable: fail softly.
    return [];
  }
  if (response.status === 404 || response.status === 501) {
    // /api/bots not yet mounted on server; keep sidebar functional.
    return [];
  }
  if (!response.ok) {
    throw new BotsApiError(await readErrorMessage(response), response.status);
  }
  const body: unknown = await response.json();
  if (Array.isArray(body)) return body as BotConfig[];
  if (typeof body === "object" && body !== null && Array.isArray((body as { bots?: unknown }).bots)) {
    return (body as { bots: BotConfig[] }).bots;
  }
  return [];
}

export async function createBot(payload: BotFormValues): Promise<BotConfig> {
  const response = await authFetch(apiUrl("/api/bots"), {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new BotsApiError(await readErrorMessage(response), response.status);
  }
  const body: unknown = await response.json();
  if (typeof body === "object" && body !== null && "bot" in body) {
    return (body as { bot: BotConfig }).bot;
  }
  return body as BotConfig;
}

export async function updateBot(id: string, payload: Partial<BotFormValues>): Promise<BotConfig> {
  const response = await authFetch(apiUrl(`/api/bots/${encodeURIComponent(id)}`), {
    method: "PATCH",
    headers: JSON_HEADERS,
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new BotsApiError(await readErrorMessage(response), response.status);
  }
  const body: unknown = await response.json();
  if (typeof body === "object" && body !== null && "bot" in body) {
    return (body as { bot: BotConfig }).bot;
  }
  return body as BotConfig;
}

export async function deleteBot(id: string): Promise<void> {
  const response = await authFetch(apiUrl(`/api/bots/${encodeURIComponent(id)}`), {
    method: "DELETE",
  });
  if (!response.ok && response.status !== 404) {
    throw new BotsApiError(await readErrorMessage(response), response.status);
  }
}
