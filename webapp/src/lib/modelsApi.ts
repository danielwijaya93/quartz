import { authFetch } from "./authToken";
import { apiUrl } from "./serverUrl";

export interface ModelProvider {
  id: string;
  label: string;
  ready: boolean;
  status: string;
  models: string[];
  efforts: string[];
  modelEfforts?: Record<string, string[]>;
  defaultModel: string;
  defaultEffort: string;
}

export interface ModelsPayload {
  current: {
    provider: string;
    model: string;
    effort: string;
  };
  providers: ModelProvider[];
}

export async function fetchModels(sessionId?: string): Promise<ModelsPayload> {
  const path = sessionId
    ? `/api/models?sessionId=${encodeURIComponent(sessionId)}`
    : "/api/models";
  const res = await authFetch(apiUrl(path));
  if (!res.ok) throw new Error(`Failed to fetch models (${res.status})`);
  return res.json() as Promise<ModelsPayload>;
}

export async function selectModel(
  provider: string,
  model?: string,
  effort?: string,
  sessionId?: string,
): Promise<{ ok: boolean; message?: string; error?: string }> {
  const res = await authFetch(apiUrl("/api/models/select"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ provider, model, effort, ...(sessionId ? { sessionId } : {}) }),
  });
  return res.json() as Promise<{ ok: boolean; message?: string; error?: string }>;
}
