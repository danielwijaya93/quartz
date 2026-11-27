import * as React from "react";
import { Input } from "@heroui/react";
import { Eye } from "lucide-react";
import { QuartzSelect } from "./QuartzSelect";
import { fetchModels, type ModelProvider } from "../lib/modelsApi";

export interface TurnWatcherSettingsCardProps {
  provider: string;
  model: string;
  antistallDefault: boolean;
  delaySeconds: string;
  defaultMaxTurns: string;
  antistallMaxNudges: string;
  defaultPrompt: string;
  saving: boolean;
  onChangeDraft: (key: string, value: string) => void;
  onToggleAntistall: () => void;
}

export function TurnWatcherSettingsCard({
  provider,
  model,
  antistallDefault,
  delaySeconds,
  defaultMaxTurns,
  antistallMaxNudges,
  defaultPrompt,
  saving,
  onChangeDraft,
  onToggleAntistall,
}: TurnWatcherSettingsCardProps) {
  const [providers, setProviders] = React.useState<ModelProvider[]>([]);

  React.useEffect(() => {
    let active = true;
    fetchModels()
      .then((res) => {
        if (active) setProviders(res.providers ?? []);
      })
      .catch(() => {
        // Best-effort dropdown suggestions; free-text inputs still work.
      });
    return () => {
      active = false;
    };
  }, []);

  const selectedProvider = providers.find((p) => p.id === provider);
  const modelOptions = selectedProvider?.models ?? [];

  return (
    <section
      aria-label="Turn Watcher"
      className="rounded-xl border border-border bg-surface/50 px-4 py-3"
    >
      <div className="mb-3 flex items-center justify-between gap-2 border-b border-separator pb-2">
        <div className="flex items-center gap-1.5">
          <Eye aria-hidden="true" className="size-3.5 text-muted" />
          <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
            Turn Watcher (Global Judge)
          </h2>
        </div>
        <span className="text-[11px] text-muted">
          {provider && model ? `${provider} / ${model}` : "Inactive until configured"}
        </span>
      </div>

      <div className="divide-y divide-separator">
        <div className="flex flex-col gap-2 pb-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <p className="text-sm font-medium text-foreground">Global watcher provider</p>
            <p className="text-xs text-muted">
              Single fast/cheap provider used to inspect finished turns across all sessions. Leave blank to keep inactive.
            </p>
          </div>
          <QuartzSelect
            aria-label="Global watcher provider"
            value={provider}
            disabled={saving}
            className="w-48 shrink-0"
            onChange={(nextProv) => {
              onChangeDraft("watcher_provider", nextProv);
              if (!nextProv) {
                onChangeDraft("watcher_model", "");
              } else {
                const match = providers.find((p) => p.id === nextProv);
                if (match && (!model || !match.models.includes(model))) {
                  onChangeDraft("watcher_model", match.defaultModel || match.models[0] || "");
                }
              }
            }}
            options={[
              { value: "", label: "(Inactive — not set)" },
              ...providers.map((p) => ({ value: p.id, label: p.label })),
              ...(provider && !providers.some((p) => p.id === provider)
                ? [{ value: provider, label: provider }]
                : []),
            ]}
          />
        </div>

        <div className="flex flex-col gap-2 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <p className="text-sm font-medium text-foreground">Global watcher model</p>
            <p className="text-xs text-muted">
              Fast, low-cost model (e.g. gemini-2.5-flash, gpt-4.1-mini, deepseek-flash).
            </p>
          </div>
          {modelOptions.length > 0 ? (
            <QuartzSelect
              aria-label="Global watcher model"
              value={model}
              disabled={saving || !provider}
              onChange={(val) => onChangeDraft("watcher_model", val)}
              className="w-48 shrink-0"
              triggerClassName="font-mono"
              options={[
                { value: "", label: "(Select model)" },
                ...modelOptions.map((m) => ({ value: m, label: m })),
                ...(model && !modelOptions.includes(model) ? [{ value: model, label: model }] : []),
              ]}
            />
          ) : (
            <Input
              aria-label="Global watcher model"
              value={model}
              placeholder="gemini-2.5-flash"
              disabled={saving || !provider}
              onChange={(e) => onChangeDraft("watcher_model", e.target.value)}
              className="w-48 shrink-0 font-mono text-xs"
            />
          )}
        </div>

        <div className="flex items-center justify-between gap-3 py-3">
          <div className="min-w-0">
            <p className="text-sm font-medium text-foreground">Anti-stall auto-continue by default</p>
            <p className="text-xs text-muted">
              When a global model is configured, gently nudge sessions that stop mid-plan.
            </p>
          </div>
          <button
            type="button"
            role="switch"
            aria-checked={antistallDefault}
            aria-label="Anti-stall auto-continue by default"
            disabled={saving}
            onClick={onToggleAntistall}
            className={`shrink-0 rounded-full px-3 py-1 text-xs font-medium transition-colors ${
              antistallDefault ? "bg-accent text-white" : "border border-border text-foreground"
            } disabled:opacity-50`}
          >
            {antistallDefault ? "On" : "Off"}
          </button>
        </div>

        <div className="flex flex-col gap-2 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <p className="text-sm font-medium text-foreground">Quiet delay (seconds)</p>
            <p className="text-xs text-muted">
              Default 30s · wait after tool calling stops before checking the last 3 messages.
            </p>
          </div>
          <Input
            aria-label="Watcher quiet delay"
            value={delaySeconds}
            onChange={(e) => onChangeDraft("watcher_delay_seconds", e.target.value)}
            className="w-28 shrink-0 font-mono text-xs"
          />
        </div>

        <div className="flex flex-col gap-2 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <p className="text-sm font-medium text-foreground">Default max turns (/goal &amp; loops)</p>
            <p className="text-xs text-muted">
              Default 10 · safety cap on consecutive auto-injected turns (1–100).
            </p>
          </div>
          <Input
            aria-label="Watcher default max turns"
            value={defaultMaxTurns}
            onChange={(e) => onChangeDraft("watcher_default_max_turns", e.target.value)}
            className="w-28 shrink-0 font-mono text-xs"
          />
        </div>

        <div className="flex flex-col gap-2 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <p className="text-sm font-medium text-foreground">Anti-stall max nudges per message</p>
            <p className="text-xs text-muted">
              Default 3 · resets whenever you send a new message (1–20).
            </p>
          </div>
          <Input
            aria-label="Watcher anti-stall max nudges"
            value={antistallMaxNudges}
            onChange={(e) => onChangeDraft("watcher_antistall_max_nudges", e.target.value)}
            className="w-28 shrink-0 font-mono text-xs"
          />
        </div>

        <div className="flex flex-col gap-2 pt-3">
          <div className="min-w-0">
            <p className="text-sm font-medium text-foreground">Default injected prompt template</p>
            <p className="text-xs text-muted">
              Supports placeholders: {"{turn}"}, {"{max_turns}"}, {"{remaining}"}, {"{goal}"}.
            </p>
          </div>
          <textarea
            aria-label="Watcher default prompt template"
            rows={2}
            value={defaultPrompt}
            disabled={saving}
            onChange={(e) => onChangeDraft("watcher_default_prompt", e.target.value)}
            className="w-full rounded-lg border border-border bg-surface px-3 py-2 font-mono text-xs text-foreground focus:border-accent focus:outline-none"
          />
        </div>
      </div>
    </section>
  );
}
