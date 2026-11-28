import * as React from "react";
import { Bot, X } from "lucide-react";
import { QuartzSelect } from "./QuartzSelect";
import { fetchModels, type ModelProvider, type ModelsPayload } from "@/lib/modelsApi";
import type { BotConfig, BotFormValues, WatcherType } from "@/types/bots";

export interface CreateBotModalProps {
  open: boolean;
  initialBot?: BotConfig | null;
  defaultCwd?: string;
  onClose: () => void;
  onSubmit: (payload: BotFormValues) => void | Promise<void>;
}

const INPUT_CLASS =
  "w-full rounded-lg border border-border bg-background px-3 py-1.5 text-sm text-foreground placeholder:text-meta focus:border-accent focus:outline-none focus:ring-1 focus:ring-focus";
const LABEL_CLASS = "block text-xs font-medium text-foreground/90 mb-1";

function splitList(raw: string): string[] {
  return raw
    .split(/[,\n]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

export function CreateBotModal({
  open,
  initialBot = null,
  defaultCwd = "",
  onClose,
  onSubmit,
}: CreateBotModalProps): React.ReactElement | null {
  const headingId = React.useId();
  const nameId = React.useId();
  const watcherId = React.useId();
  const cwdId = React.useId();
  const instructionId = React.useId();
  const autoAllowId = React.useId();
  const repoId = React.useId();
  const eventsId = React.useId();
  const intervalId = React.useId();
  const cronExprId = React.useId();
  const timezoneId = React.useId();
  const secretId = React.useId();
  const filterId = React.useId();
  const providerId = React.useId();
  const modelId = React.useId();
  const effortId = React.useId();
  const worktreeId = React.useId();
  const maxErrorsId = React.useId();

  const isEditing = Boolean(initialBot);

  const [name, setName] = React.useState("");
  const [watcherType, setWatcherType] = React.useState<WatcherType>("github_poll");
  const [cwd, setCwd] = React.useState("");
  const [provider, setProvider] = React.useState("");
  const [model, setModel] = React.useState("");
  const [effort, setEffort] = React.useState("");
  const [modelsData, setModelsData] = React.useState<ModelsPayload | null>(null);
  const [instruction, setInstruction] = React.useState("");
  const [autoAllowText, setAutoAllowText] = React.useState("");
  const [enabled, setEnabled] = React.useState(true);
  const [useWorktree, setUseWorktree] = React.useState(false);
  const [maxConsecutiveErrors, setMaxConsecutiveErrors] = React.useState("");

  // GitHub Poll fields
  const [githubRepo, setGithubRepo] = React.useState("");
  const [githubEvents, setGithubEvents] = React.useState("issues");
  const [githubInterval, setGithubInterval] = React.useState("900");

  // Cron fields
  const [cronExpression, setCronExpression] = React.useState("");
  const [cronTimezone, setCronTimezone] = React.useState("UTC");

  // Webhook fields
  const [webhookSecret, setWebhookSecret] = React.useState("");
  const [webhookFilter, setWebhookFilter] = React.useState("");

  const [error, setError] = React.useState<string | null>(null);
  const [submitting, setSubmitting] = React.useState(false);

  React.useEffect(() => {
    if (!open) return;
    setError(null);
    setSubmitting(false);
    if (initialBot) {
      setName(initialBot.name);
      setWatcherType(initialBot.watcher_type);
      setCwd(initialBot.cwd ?? "");
      setProvider(initialBot.provider ?? "");
      setModel(initialBot.model ?? "");
      setEffort(initialBot.effort ?? "");
      setInstruction(initialBot.instruction);
      setAutoAllowText(initialBot.permissions.auto_allow.join(", "));
      setEnabled(initialBot.enabled);
      setUseWorktree(initialBot.use_worktree);
      setMaxConsecutiveErrors(
        initialBot.max_consecutive_errors != null ? String(initialBot.max_consecutive_errors) : "",
      );
      setGithubRepo(initialBot.github?.repo ?? "");
      setGithubEvents(initialBot.github?.events.join(", ") ?? "issues");
      setGithubInterval(String(initialBot.github?.interval_seconds ?? 900));
      setCronExpression(initialBot.cron?.expression ?? "");
      setCronTimezone(initialBot.cron?.timezone ?? "UTC");
      setWebhookSecret(initialBot.webhook?.secret ?? "");
      setWebhookFilter(initialBot.webhook?.event_filter ?? "");
    } else {
      setName("");
      setWatcherType("github_poll");
      setCwd(defaultCwd);
      setProvider("");
      setModel("");
      setEffort("");
      setInstruction("");
      setAutoAllowText("");
      setEnabled(true);
      setUseWorktree(false);
      setMaxConsecutiveErrors("");
      setGithubRepo("");
      setGithubEvents("issues");
      setGithubInterval("900");
      setCronExpression("");
      setCronTimezone("UTC");
      setWebhookSecret("");
      setWebhookFilter("");
    }
  }, [open, initialBot, defaultCwd]);

  React.useEffect(() => {
    if (!open) return;
    let cancelled = false;
    void fetchModels()
      .then((payload) => {
        if (!cancelled) setModelsData(payload);
      })
      .catch(() => {
        // Best-effort: modal remains functional with Default (inherit) when offline.
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  const providers = modelsData?.providers ?? [];
  const activeProvider: ModelProvider | undefined = React.useMemo(
    () => providers.find((p) => p.id === provider),
    [providers, provider],
  );
  const availableModels: string[] = React.useMemo(() => {
    const base = activeProvider?.models ?? [];
    if (model && !base.includes(model)) return [model, ...base];
    return base;
  }, [activeProvider, model]);
  const availableEfforts: string[] = React.useMemo(() => {
    const fallbackProvider =
      activeProvider ?? providers.find((p) => p.id === modelsData?.current?.provider);
    const perModel = model ? fallbackProvider?.modelEfforts?.[model] : undefined;
    const base =
      perModel && perModel.length > 0
        ? perModel
        : (fallbackProvider?.efforts ?? ["low", "medium", "high"]);
    if (effort && !base.includes(effort)) return [effort, ...base];
    return base;
  }, [activeProvider, providers, modelsData, model, effort]);

  React.useEffect(() => {
    if (!open) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !e.defaultPrevented) {
        onClose();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose]);

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>): Promise<void> => {
    e.preventDefault();
    setError(null);

    const trimmedName = name.trim();
    const trimmedInstruction = instruction.trim();
    if (!trimmedName) {
      setError("Bot Name is required.");
      return;
    }
    if (!trimmedInstruction) {
      setError("Instruction is required.");
      return;
    }

    if (watcherType === "github_poll" && !githubRepo.trim()) {
      setError("GitHub Repo (owner/repo) is required.");
      return;
    }
    if (watcherType === "cron" && !cronExpression.trim()) {
      setError("Cron Expression is required.");
      return;
    }
    if (watcherType === "webhook" && !webhookSecret.trim()) {
      setError("Webhook Secret is required.");
      return;
    }

    const parsedInterval = Number.parseInt(githubInterval, 10);
    const intervalSeconds = Number.isFinite(parsedInterval) && parsedInterval > 0 ? parsedInterval : 900;
    const parsedEvents = splitList(githubEvents);

    let parsedMaxErrors: number | undefined;
    if (maxConsecutiveErrors.trim() !== "") {
      const val = Number.parseInt(maxConsecutiveErrors.trim(), 10);
      if (!Number.isFinite(val) || val < 0) {
        setError("Max Consecutive Errors must be a non-negative integer.");
        return;
      }
      parsedMaxErrors = val;
    }

    const payload: BotFormValues = {
      ...(initialBot ? { id: initialBot.id } : {}),
      name: trimmedName,
      watcher_type: watcherType,
      instruction: trimmedInstruction,
      cwd: cwd.trim() ? cwd.trim() : null,
      provider: provider.trim() ? provider.trim() : null,
      model: model.trim() ? model.trim() : null,
      effort: effort.trim() ? effort.trim() : null,
      enabled,
      use_worktree: useWorktree,
      max_queue_depth: initialBot?.max_queue_depth ?? 20,
      ...(parsedMaxErrors !== undefined ? { max_consecutive_errors: parsedMaxErrors } : {}),
      github:
        watcherType === "github_poll"
          ? {
              repo: githubRepo.trim(),
              events: parsedEvents.length > 0 ? parsedEvents : ["issues"],
              interval_seconds: intervalSeconds,
            }
          : null,
      cron:
        watcherType === "cron"
          ? {
              expression: cronExpression.trim(),
              timezone: cronTimezone.trim() || "UTC",
            }
          : null,
      webhook:
        watcherType === "webhook"
          ? {
              secret: webhookSecret.trim(),
              event_filter: webhookFilter.trim(),
            }
          : null,
      permissions: {
        auto_allow: splitList(autoAllowText),
        require_approval: initialBot?.permissions.require_approval ?? ["git push", "gh pr create"],
        grace_seconds: initialBot?.permissions.grace_seconds ?? 15,
      },
    };

    setSubmitting(true);
    try {
      await onSubmit(payload);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save bot.");
    } finally {
      setSubmitting(false);
    }
  };

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center overflow-y-auto bg-black/50 p-4 backdrop-blur-xs"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
        className="my-auto w-[min(34rem,calc(100vw-2rem))] rounded-2xl border border-border bg-surface p-6 text-foreground shadow-2xl"
      >
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <div className="flex size-8 shrink-0 items-center justify-center rounded-xl bg-accent/15 text-accent">
            <Bot aria-hidden="true" className="size-4" />
          </div>
          <h2 id={headingId} className="text-base font-semibold">
            {isEditing ? "Edit Bot" : "Create Bot"}
          </h2>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="flex size-7 cursor-pointer items-center justify-center rounded-lg text-muted transition-colors hover:bg-surface-secondary hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-focus"
        >
          <X aria-hidden="true" className="size-4" />
        </button>
      </div>

      <form onSubmit={(e) => void handleSubmit(e)} noValidate className="mt-4 space-y-3.5">
        {error ? (
          <div
            role="alert"
            className="rounded-lg border border-danger/40 bg-danger-soft px-3 py-2 text-xs text-danger-soft-foreground"
          >
            {error}
          </div>
        ) : null}

        <div>
          <label htmlFor={nameId} className={LABEL_CLASS}>
            Bot Name
          </label>
          <input
            id={nameId}
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Issue Triager"
            required
            className={INPUT_CLASS}
          />
        </div>

        <div>
          <span id={watcherId} className={LABEL_CLASS}>
            Watcher Type
          </span>
          <QuartzSelect
            aria-labelledby={watcherId}
            value={watcherType}
            onChange={(val) => setWatcherType(val as WatcherType)}
            options={[
              { value: "github_poll", label: "GitHub Poll" },
              { value: "cron", label: "Cron" },
              { value: "webhook", label: "Webhook" },
            ]}
          />
        </div>

        {watcherType === "github_poll" ? (
          <div className="space-y-3 rounded-xl border border-separator bg-surface-secondary/40 p-3">
            <div>
              <label htmlFor={repoId} className={LABEL_CLASS}>
                GitHub Repo
              </label>
              <input
                id={repoId}
                type="text"
                value={githubRepo}
                onChange={(e) => setGithubRepo(e.target.value)}
                placeholder="owner/repo"
                required
                className={INPUT_CLASS}
              />
            </div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <div>
                <label htmlFor={eventsId} className={LABEL_CLASS}>
                  Events
                </label>
                <input
                  id={eventsId}
                  type="text"
                  value={githubEvents}
                  onChange={(e) => setGithubEvents(e.target.value)}
                  placeholder="issues, pull_request"
                  className={INPUT_CLASS}
                />
              </div>
              <div>
                <label htmlFor={intervalId} className={LABEL_CLASS}>
                  Poll Interval (seconds)
                </label>
                <input
                  id={intervalId}
                  type="number"
                  min={30}
                  value={githubInterval}
                  onChange={(e) => setGithubInterval(e.target.value)}
                  className={INPUT_CLASS}
                />
              </div>
            </div>
          </div>
        ) : null}

        {watcherType === "cron" ? (
          <div className="grid grid-cols-1 gap-3 rounded-xl border border-separator bg-surface-secondary/40 p-3 sm:grid-cols-2">
            <div>
              <label htmlFor={cronExprId} className={LABEL_CLASS}>
                Cron Expression
              </label>
              <input
                id={cronExprId}
                type="text"
                value={cronExpression}
                onChange={(e) => setCronExpression(e.target.value)}
                placeholder="0 9 * * *"
                required
                className={INPUT_CLASS}
              />
            </div>
            <div>
              <label htmlFor={timezoneId} className={LABEL_CLASS}>
                Timezone
              </label>
              <input
                id={timezoneId}
                type="text"
                value={cronTimezone}
                onChange={(e) => setCronTimezone(e.target.value)}
                placeholder="UTC"
                className={INPUT_CLASS}
              />
            </div>
          </div>
        ) : null}

        {watcherType === "webhook" ? (
          <div className="grid grid-cols-1 gap-3 rounded-xl border border-separator bg-surface-secondary/40 p-3 sm:grid-cols-2">
            <div>
              <label htmlFor={secretId} className={LABEL_CLASS}>
                Webhook Secret
              </label>
              <input
                id={secretId}
                type="password"
                value={webhookSecret}
                onChange={(e) => setWebhookSecret(e.target.value)}
                placeholder="whsec_..."
                required
                className={INPUT_CLASS}
              />
            </div>
            <div>
              <label htmlFor={filterId} className={LABEL_CLASS}>
                Event Filter
              </label>
              <input
                id={filterId}
                type="text"
                value={webhookFilter}
                onChange={(e) => setWebhookFilter(e.target.value)}
                placeholder="issues.opened"
                className={INPUT_CLASS}
              />
            </div>
          </div>
        ) : null}

        <div>
          <label htmlFor={cwdId} className={LABEL_CLASS}>
            Working Directory (CWD)
          </label>
          <input
            id={cwdId}
            type="text"
            value={cwd}
            onChange={(e) => setCwd(e.target.value)}
            placeholder="/path/to/repo"
            className={INPUT_CLASS}
          />
        </div>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <div>
            <span id={providerId} className={LABEL_CLASS}>
              Provider
            </span>
            <QuartzSelect
              aria-labelledby={providerId}
              value={provider}
              onChange={(next) => {
                setProvider(next);
                if (!next) {
                  setModel("");
                  setEffort("");
                } else {
                  const nextProv = providers.find((p) => p.id === next);
                  if (nextProv && model && !nextProv.models.includes(model)) {
                    setModel("");
                  }
                  if (nextProv && effort && !nextProv.efforts.includes(effort)) {
                    setEffort("");
                  }
                }
              }}
              options={[
                { value: "", label: "Default (inherit)" },
                ...(provider && !providers.some((p) => p.id === provider)
                  ? [{ value: provider, label: provider }]
                  : []),
                ...providers.map((p) => ({ value: p.id, label: p.label || p.id })),
              ]}
            />
          </div>
          <div>
            <span id={modelId} className={LABEL_CLASS}>
              Model
            </span>
            <QuartzSelect
              aria-labelledby={modelId}
              value={model}
              disabled={!provider && !model}
              onChange={(nextModel) => {
                setModel(nextModel);
                const perModel = nextModel ? activeProvider?.modelEfforts?.[nextModel] : undefined;
                const nextEfforts =
                  perModel && perModel.length > 0 ? perModel : activeProvider?.efforts;
                if (nextEfforts && effort && !nextEfforts.includes(effort)) {
                  setEffort("");
                }
              }}
              options={[
                { value: "", label: "Default" },
                ...availableModels.map((m) => ({ value: m, label: m })),
              ]}
            />
          </div>
          <div>
            <span id={effortId} className={LABEL_CLASS}>
              Effort
            </span>
            <QuartzSelect
              aria-labelledby={effortId}
              value={effort}
              onChange={setEffort}
              options={[
                { value: "", label: "Default" },
                ...availableEfforts.map((level) => ({ value: level, label: level })),
              ]}
            />
          </div>
        </div>

        <div>
          <label htmlFor={instructionId} className={LABEL_CLASS}>
            Instruction
          </label>
          <textarea
            id={instructionId}
            rows={3}
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
            placeholder="Describe what the bot should do when triggered..."
            required
            className={INPUT_CLASS}
          />
        </div>

        <div>
          <label htmlFor={autoAllowId} className={LABEL_CLASS}>
            Pre-approved Commands
          </label>
          <input
            id={autoAllowId}
            type="text"
            value={autoAllowText}
            onChange={(e) => setAutoAllowText(e.target.value)}
            placeholder="git status, pytest"
            className={INPUT_CLASS}
          />
        </div>

        <div className="rounded-xl border border-separator bg-surface-secondary/30 p-3">
          <label htmlFor={maxErrorsId} className={LABEL_CLASS}>
            Max Consecutive Errors (auto-pause, 0 disables)
          </label>
          <input
            id={maxErrorsId}
            type="number"
            min={0}
            value={maxConsecutiveErrors}
            onChange={(e) => setMaxConsecutiveErrors(e.target.value)}
            placeholder="Default (5)"
            className={INPUT_CLASS}
          />
        </div>

        <label htmlFor={worktreeId} className="flex cursor-pointer items-center gap-2 text-xs text-foreground">
          <input
            id={worktreeId}
            type="checkbox"
            checked={useWorktree}
            onChange={(e) => setUseWorktree(e.target.checked)}
            className="size-3.5 rounded border-border accent-accent"
          />
          Run in isolated git worktree
        </label>

        <div className="flex flex-col-reverse gap-2 pt-2 sm:flex-row sm:justify-end">
          <button
            type="button"
            onClick={onClose}
            className="cursor-pointer rounded-lg border border-border bg-surface-secondary px-3.5 py-1.5 text-sm font-medium text-foreground transition-colors hover:bg-surface-secondary/80 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-focus"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={submitting}
            className="cursor-pointer rounded-lg bg-accent px-4 py-1.5 text-sm font-medium text-accent-foreground transition-opacity hover:opacity-90 disabled:opacity-50 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-focus"
          >
            {isEditing ? "Save Changes" : "Create Bot"}
          </button>
        </div>
      </form>
      </div>
    </div>
  );
}
