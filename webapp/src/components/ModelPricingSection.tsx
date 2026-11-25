import * as React from "react";
import { Button, Input } from "@heroui/react";
import { DollarSign, Plus, Trash2 } from "lucide-react";

export interface ModelPricingRuleItem {
  pattern: string;
  inputPerM: string;
  cachedInputPerM: string;
  outputPerM: string;
}

export interface RecentModelSuggestion {
  provider: string;
  model: string;
}

interface ModelPricingSectionProps {
  rules: ModelPricingRuleItem[];
  onChangeRules: (next: ModelPricingRuleItem[]) => void;
  fallbackInput: string;
  fallbackOutput: string;
  onChangeFallbackInput: (val: string) => void;
  onChangeFallbackOutput: (val: string) => void;
  trackChatUsage: boolean;
  onToggleTrackChatUsage: () => void;
  recentModels: RecentModelSuggestion[];
  saving: boolean;
}

export function ModelPricingSection({
  rules,
  onChangeRules,
  fallbackInput,
  fallbackOutput,
  onChangeFallbackInput,
  onChangeFallbackOutput,
  trackChatUsage,
  onToggleTrackChatUsage,
  recentModels,
  saving,
}: ModelPricingSectionProps): React.ReactElement {
  const existingPatterns = React.useMemo(
    () => new Set(rules.map((r) => r.pattern.trim().toLowerCase())),
    [rules],
  );

  const suggestions = React.useMemo(
    () =>
      recentModels.filter((m) => {
        const key = m.provider ? `${m.provider}/${m.model}` : m.model;
        return m.model && !existingPatterns.has(key.toLowerCase()) && !existingPatterns.has(m.model.toLowerCase());
      }),
    [recentModels, existingPatterns],
  );

  const addRule = (pattern = ""): void => {
    onChangeRules([
      ...rules,
      { pattern, inputPerM: "0", cachedInputPerM: "0", outputPerM: "0" },
    ]);
  };

  const updateRule = (idx: number, patch: Partial<ModelPricingRuleItem>): void => {
    onChangeRules(rules.map((r, i) => (i === idx ? { ...r, ...patch } : r)));
  };

  const removeRule = (idx: number): void => {
    onChangeRules(rules.filter((_, i) => i !== idx));
  };

  return (
    <section
      aria-label="Model Pricing and Cost Tracking"
      className="rounded-xl border border-border bg-surface/50 px-4 py-3"
    >
      <div className="mb-3 flex items-center justify-between gap-2 border-b border-separator pb-2">
        <div className="flex items-center gap-1.5">
          <DollarSign aria-hidden="true" className="size-3.5 text-muted" />
          <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
            Model Pricing &amp; Cost Tracking
          </h2>
        </div>
        <Button
          size="sm"
          variant="outline"
          isDisabled={saving}
          onPress={() => addRule("")}
        >
          <Plus aria-hidden="true" className="size-3.5" />
          Add model
        </Button>
      </div>

      <div className="divide-y divide-separator">
        <div className="flex items-center justify-between gap-3 pb-3">
          <div className="min-w-0">
            <p className="text-sm font-medium text-foreground">Track interactive chat usage</p>
            <p className="text-xs text-muted">
              Log web chat turns alongside autonomous bot runs in 24h spend metrics.
            </p>
          </div>
          <button
            type="button"
            role="switch"
            aria-checked={trackChatUsage}
            aria-label="Track interactive chat usage"
            disabled={saving}
            onClick={onToggleTrackChatUsage}
            className={`shrink-0 rounded-full px-3 py-1 text-xs font-medium transition-colors ${
              trackChatUsage ? "bg-accent text-white" : "border border-border text-foreground"
            } disabled:opacity-50`}
          >
            {trackChatUsage ? "On" : "Off"}
          </button>
        </div>

        <div className="flex flex-col gap-2 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <p className="text-sm font-medium text-foreground">Unpriced model fallback ($ / 1M tokens)</p>
            <p className="text-xs text-muted">
              Used when a provider doesn&apos;t return cost and no rule matches below (0 disables).
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Input
              aria-label="Fallback input rate ($/1M)"
              value={fallbackInput}
              onChange={(e) => onChangeFallbackInput(e.target.value)}
              placeholder="1.0"
              className="w-20 font-mono text-xs"
            />
            <span className="text-xs text-muted">in /</span>
            <Input
              aria-label="Fallback output rate ($/1M)"
              value={fallbackOutput}
              onChange={(e) => onChangeFallbackOutput(e.target.value)}
              placeholder="5.0"
              className="w-20 font-mono text-xs"
            />
            <span className="text-xs text-muted">out</span>
          </div>
        </div>

        <div className="pt-3">
          <p className="mb-2 text-xs text-muted">
            Rates per 1M tokens (exact or wildcard like <code className="font-mono">google/gemini-4-*</code>). Cache writes bill at the Input rate.
          </p>

          {suggestions.length > 0 ? (
            <div className="mb-3 flex flex-wrap items-center gap-1.5">
              <span className="text-xs text-muted">Recent models:</span>
              {suggestions.slice(0, 6).map((s) => {
                const pat = s.provider ? `${s.provider}/${s.model}` : s.model;
                return (
                  <button
                    key={pat}
                    type="button"
                    onClick={() => addRule(pat)}
                    className="rounded-md border border-border bg-surface px-2 py-0.5 font-mono text-xs text-foreground hover:bg-surface/80"
                  >
                    + {pat}
                  </button>
                );
              })}
            </div>
          ) : null}

          {rules.length === 0 ? (
            <p className="rounded-lg border border-dashed border-border px-3 py-3 text-xs text-muted">
              No custom model rates configured yet. Unpriced models use the fallback rate above.
            </p>
          ) : (
            <div className="space-y-2">
              <div className="hidden grid-cols-[minmax(0,1fr)_5.5rem_6rem_5.5rem_2rem] gap-2 text-[11px] font-medium uppercase tracking-wide text-muted sm:grid">
                <span>Provider / Model</span>
                <span>Input</span>
                <span>Cached In</span>
                <span>Output</span>
                <span />
              </div>
              {rules.map((rule, idx) => (
                <div
                  key={idx}
                  className="grid grid-cols-1 gap-2 rounded-lg border border-border/60 bg-background/40 p-2 sm:grid-cols-[minmax(0,1fr)_5.5rem_6rem_5.5rem_2rem] sm:items-center sm:border-0 sm:bg-transparent sm:p-0"
                >
                  <Input
                    aria-label={`Model pattern ${idx + 1}`}
                    value={rule.pattern}
                    placeholder="google/gemini-4-hydrogen"
                    onChange={(e) => updateRule(idx, { pattern: e.target.value })}
                    className="font-mono text-xs"
                  />
                  <Input
                    aria-label={`Input $/1M ${idx + 1}`}
                    value={rule.inputPerM}
                    placeholder="0.50"
                    onChange={(e) => updateRule(idx, { inputPerM: e.target.value })}
                    className="font-mono text-xs"
                  />
                  <Input
                    aria-label={`Cached input $/1M ${idx + 1}`}
                    value={rule.cachedInputPerM}
                    placeholder="0.125"
                    onChange={(e) => updateRule(idx, { cachedInputPerM: e.target.value })}
                    className="font-mono text-xs"
                  />
                  <Input
                    aria-label={`Output $/1M ${idx + 1}`}
                    value={rule.outputPerM}
                    placeholder="2.00"
                    onChange={(e) => updateRule(idx, { outputPerM: e.target.value })}
                    className="font-mono text-xs"
                  />
                  <button
                    type="button"
                    aria-label={`Remove model rule ${idx + 1}`}
                    onClick={() => removeRule(idx)}
                    className="flex size-8 items-center justify-center rounded-md text-muted hover:bg-danger/10 hover:text-danger"
                  >
                    <Trash2 aria-hidden="true" className="size-3.5" />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
