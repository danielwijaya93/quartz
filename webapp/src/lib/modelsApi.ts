export interface ModelProvider {
  id: string;
  label: string;
  ready: boolean;
  status: string;
  models: string[];
  efforts: string[];
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

export async function fetchModels(): Promise<ModelsPayload> {
  const res = await fetch("/api/models");
  if (!res.ok) throw new Error(`Failed to fetch models (${res.status})`);
  return res.json() as Promise<ModelsPayload>;
}

export async function selectModel(
  provider: string,
  model?: string,
  effort?: string,
): Promise<{ ok: boolean; message?: string; error?: string }> {
  const res = await fetch("/api/models/select", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ provider, model, effort }),
  });
  return res.json() as Promise<{ ok: boolean; message?: string; error?: string }>;
}
