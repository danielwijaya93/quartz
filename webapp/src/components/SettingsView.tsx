import * as React from "react";
import { ServerUrlCard } from "./ServerUrlCard";
import { apiUrl, isNativeCapacitor } from "../lib/serverUrl";
import { NativePlugin } from "../lib/nativeBridge";
import { BiometricLockCard } from "./BiometricLockCard";
import { TurnWatcherSettingsCard } from "./TurnWatcherSettingsCard";
import { authFetch } from "../lib/authToken";
import { Button, Input } from "@heroui/react";
import { Bell, Bot, MessageSquare, RotateCcw, Save, Shield } from "lucide-react";
import type { NotificationCategory } from "../lib/notifications";
import { useNotifications, type NotificationsController } from "../lib/useNotifications";
import { CanvasPage } from "./CanvasPage";

type SettingsTabId = "general" | "bots" | "session" | "advanced";

const SETTINGS_TABS: readonly { id: SettingsTabId; label: string }[] = [
  { id: "general", label: "General & Alerts" },
  { id: "bots", label: "Bots" },
  { id: "session", label: "Session & Context" },
  { id: "advanced", label: "Advanced" },
];

const NOTIFICATION_CATEGORIES: readonly {
  key: NotificationCategory;
  label: string;
  description: string;
}[] = [
  { key: "turn", label: "Turn updates", description: "Notify when a turn finishes or fails." },
  { key: "approval", label: "Approval requests", description: "Notify when a command or bot needs approval." },
  { key: "background", label: "Background jobs", description: "Notify when a background job finishes." },
  { key: "error", label: "Errors", description: "Notify on session or runtime errors." },
];

interface SettingsValues {
  reserve: number;
  keep: number;
  progress: number;
  bg: number;
  idle?: number;
  sandboxEnvPassthrough?: string;
  botMaxConsecutiveErrors?: number;
  watcherProvider?: string;
  watcherModel?: string;
  watcherAntistallDefault?: boolean;
  watcherDelaySeconds?: number;
  watcherDefaultMaxTurns?: number;
  watcherAntistallMaxNudges?: number;
  watcherDefaultPrompt?: string;
  autoRenameEnabled?: boolean;
}

interface SettingsPayload {
  values: SettingsValues;
  defaults: SettingsValues;
  configPath: string;
}

interface SettingFieldDef {
  apiKey: string;
  valueKey: keyof SettingsValues;
  label: string;
  hint: (defaults: SettingsValues) => string;
  placeholder?: string;
  allowEmpty?: boolean;
  emptySubmitValue?: string;
  inputWidth?: string;
}

const SESSION_FIELDS: readonly SettingFieldDef[] = [
  { apiKey: "reserve", valueKey: "reserve", label: "Reserve tokens", hint: (d) => `Default ${(d.reserve ?? 16384).toLocaleString()}` },
  { apiKey: "keep", valueKey: "keep", label: "Keep recent tokens", hint: (d) => `Default ${(d.keep ?? 32000).toLocaleString()}` },
  { apiKey: "progress", valueKey: "progress", label: "Progress update every (calls)", hint: (d) => `Default ${d.progress ?? 5} · 0 disables` },
  { apiKey: "bg", valueKey: "bg", label: "Background job check (seconds)", hint: (d) => `Default ${d.bg ?? 300}s · 0 disables` },
  { apiKey: "idle", valueKey: "idle", label: "Idle session auto-close (hours)", hint: (d) => `Default ${d.idle ?? 2}h · 0 disables` },
];

const BOT_GOVERNANCE_FIELDS: readonly SettingFieldDef[] = [
  {
    apiKey: "bot_errors",
    valueKey: "botMaxConsecutiveErrors",
    label: "Default max consecutive errors",
    hint: (d) => `Default ${d.botMaxConsecutiveErrors ?? 5} · auto-pauses bot (0 disables)`,
  },
];

const ALL_FIELD_DEFS: readonly SettingFieldDef[] = [
  ...SESSION_FIELDS,
  ...BOT_GOVERNANCE_FIELDS,
];

function formatValueForDraft(val: unknown, fallback: unknown): string {
  const effective = val === undefined ? fallback : val;
  if (effective === null || effective === undefined) return "";
  return String(effective);
}

function draftsFromValues(values: SettingsValues, defaults: SettingsValues): Record<string, string> {
  return {
    reserve: formatValueForDraft(values.reserve, defaults.reserve ?? 16384),
    keep: formatValueForDraft(values.keep, defaults.keep ?? 32000),
    progress: formatValueForDraft(values.progress, defaults.progress ?? 5),
    bg: formatValueForDraft(values.bg, defaults.bg ?? 300),
    idle: formatValueForDraft(values.idle, defaults.idle ?? 2),
    sandbox_env: formatValueForDraft(values.sandboxEnvPassthrough, defaults.sandboxEnvPassthrough ?? ""),
    bot_errors: formatValueForDraft(values.botMaxConsecutiveErrors, defaults.botMaxConsecutiveErrors ?? 5),
    watcher_provider: formatValueForDraft(values.watcherProvider, defaults.watcherProvider ?? ""),
    watcher_model: formatValueForDraft(values.watcherModel, defaults.watcherModel ?? ""),
    watcher_delay_seconds: formatValueForDraft(values.watcherDelaySeconds, defaults.watcherDelaySeconds ?? 30),
    watcher_default_max_turns: formatValueForDraft(values.watcherDefaultMaxTurns, defaults.watcherDefaultMaxTurns ?? 10),
    watcher_antistall_max_nudges: formatValueForDraft(values.watcherAntistallMaxNudges, defaults.watcherAntistallMaxNudges ?? 3),
    watcher_default_prompt: formatValueForDraft(values.watcherDefaultPrompt, defaults.watcherDefaultPrompt ?? "Continue with the next step you mentioned."),
  };
}

function notificationStatusText(notifications: NotificationsController, isNative: boolean): string {
  const { env, enabled } = notifications;
  if (!env.supported) {
    return "Notifications are not supported in this browser.";
  }
  if (env.permission === "denied") {
    return isNative
      ? "Blocked in Android settings. Re-enable notifications for Quartz in system settings."
      : "Blocked in browser settings. Re-enable notifications for this site to receive alerts.";
  }
  if (enabled && env.permission === "granted") {
    return isNative
      ? "Active on this device when Quartz is in the background."
      : "Active for this browser when Quartz is in the background.";
  }
  if (env.permission === "default") {
    return "Allow notifications to get alerts when Quartz is in the background.";
  }
  return isNative ? "Paused on this device." : "Paused for this browser.";
}

export function SettingsView({
  onBack,
  notifications: externalNotifications,
}: {
  onBack: () => void;
  notifications?: NotificationsController;
}): React.ReactElement {
  const fallbackNotifications = useNotifications();
  const notifications = externalNotifications ?? fallbackNotifications;
  const [activeTab, setActiveTab] = React.useState<SettingsTabId>("general");
  const [data, setData] = React.useState<SettingsPayload | null>(null);
  const [drafts, setDrafts] = React.useState<Record<string, string>>({});
  const [loading, setLoading] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [status, setStatus] = React.useState<{ ok: boolean; text: string } | null>(null);
  const [serverVersion, setServerVersion] = React.useState<string | null>(null);
  const [batteryUnrestricted, setBatteryUnrestricted] = React.useState<boolean | null>(null);
  const isNative = isNativeCapacitor();

  const applyPayloadValues = React.useCallback((values: SettingsValues, defaults: SettingsValues) => {
    setDrafts(draftsFromValues(values, defaults));
  }, []);

  const load = React.useCallback(async () => {
    setLoading(true);
    setStatus(null);
    try {
      const [resp, healthResp] = await Promise.all([
        authFetch(apiUrl("/api/settings")),
        fetch(apiUrl("/api/health")).catch(() => null),
      ]);
      if (healthResp?.ok) {
        const health = (await healthResp.json()) as { version?: unknown };
        if (typeof health.version === "string" && health.version) setServerVersion(health.version);
      }
      if (!resp.ok) throw new Error("Failed to load settings");
      const payload = (await resp.json()) as SettingsPayload;
      setData(payload);
      applyPayloadValues(payload.values, payload.defaults);
    } catch {
      setStatus({ ok: false, text: "Could not load settings." });
    } finally {
      setLoading(false);
    }
  }, [applyPayloadValues]);

  React.useEffect(() => {
    void load();
  }, [load]);

  React.useEffect(() => {
    if (!isNative) return;
    const checkBattery = (): void => {
      void NativePlugin.batteryOptimizationStatus()
        .then((res) => setBatteryUnrestricted(Boolean(res?.ignored)))
        .catch(() => setBatteryUnrestricted(null));
    };
    checkBattery();
    window.addEventListener("focus", checkBattery);
    return () => window.removeEventListener("focus", checkBattery);
  }, [isNative]);

  const saveSingleKey = async (key: string, value?: unknown): Promise<void> => {
    setSaving(true);
    setStatus(null);
    try {
      const resp = await authFetch(apiUrl("/api/settings"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(key === "reset" ? { key: "reset" } : { key, value }),
      });
      const body = (await resp.json()) as {
        ok?: boolean;
        error?: string;
        message?: string;
        values?: SettingsValues;
      };
      if (!resp.ok || !body.ok) {
        setStatus({ ok: false, text: body.error || "Failed to save setting." });
        return;
      }
      if (body.values) {
        setData((prev) => (prev ? { ...prev, values: body.values! } : prev));
        applyPayloadValues(body.values, data?.defaults ?? body.values);
      }
      setStatus({ ok: true, text: body.message || "Saved." });
    } catch {
      setStatus({ ok: false, text: "Request failed." });
    } finally {
      setSaving(false);
    }
  };

  const dirtyUpdates = React.useMemo((): Record<string, unknown> => {
    if (!data) return {};
    const updates: Record<string, unknown> = {};
    for (const field of ALL_FIELD_DEFS) {
      const current = formatValueForDraft(data.values[field.valueKey], data.defaults[field.valueKey]);
      const val = drafts[field.apiKey] ?? current;
      if (val.trim() !== current) {
        updates[field.apiKey] = val.trim() === "" && field.allowEmpty ? (field.emptySubmitValue ?? "none") : val.trim();
      }
    }
    const sandboxCur = data.values.sandboxEnvPassthrough ?? "";
    const sandboxDraft = (drafts.sandbox_env ?? sandboxCur).trim();
    if (sandboxDraft !== sandboxCur) {
      updates.sandbox_env = sandboxDraft || "none";
    }
    const watcherDraftSpecs: readonly [string, keyof SettingsValues, string][] = [
      ["watcher_provider", "watcherProvider", "none"],
      ["watcher_model", "watcherModel", "none"],
      ["watcher_delay_seconds", "watcherDelaySeconds", "30"],
      ["watcher_default_max_turns", "watcherDefaultMaxTurns", "10"],
      ["watcher_antistall_max_nudges", "watcherAntistallMaxNudges", "3"],
      ["watcher_default_prompt", "watcherDefaultPrompt", "default"],
    ];
    for (const [apiKey, valueKey, emptyVal] of watcherDraftSpecs) {
      const current = formatValueForDraft(data.values[valueKey], data.defaults[valueKey] ?? "");
      const draftVal = (drafts[apiKey] ?? current).trim();
      if (draftVal !== current) {
        updates[apiKey] = draftVal === "" ? emptyVal : draftVal;
      }
    }
    return updates;
  }, [data, drafts]);

  const dirtyCount = Object.keys(dirtyUpdates).length;

  const saveAllDirty = async (): Promise<void> => {
    if (dirtyCount === 0) return;
    setSaving(true);
    setStatus(null);
    try {
      const resp = await authFetch(apiUrl("/api/settings"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ updates: dirtyUpdates }),
      });
      const body = (await resp.json()) as {
        ok?: boolean;
        error?: string;
        message?: string;
        values?: SettingsValues;
      };
      if (!resp.ok || !body.ok) {
        setStatus({ ok: false, text: body.error || "Failed to save settings." });
        return;
      }
      if (body.values) {
        setData((prev) => (prev ? { ...prev, values: body.values! } : prev));
        applyPayloadValues(body.values, data?.defaults ?? body.values);
      }
      setStatus({ ok: true, text: body.message || "Saved." });
    } catch {
      setStatus({ ok: false, text: "Request failed." });
    } finally {
      setSaving(false);
    }
  };

  const renderFieldRows = (fields: readonly SettingFieldDef[]): React.ReactElement | null => {
    if (!data) return null;
    return (
      <div className="divide-y divide-separator">
        {fields.map((field) => {
          const current = formatValueForDraft(data.values[field.valueKey], data.defaults[field.valueKey]);
          const val = drafts[field.apiKey] ?? current;
          return (
            <div
              key={field.apiKey}
              className="flex flex-col gap-2 py-3 first:pt-0 last:pb-0 sm:flex-row sm:items-center sm:justify-between"
            >
              <div className="min-w-0">
                <p className="text-sm font-medium text-foreground">{field.label}</p>
                <p className="text-xs text-muted">{field.hint(data.defaults)}</p>
              </div>
              <Input
                aria-label={field.label}
                value={val}
                placeholder={field.placeholder}
                onChange={(e) => setDrafts((prev) => ({ ...prev, [field.apiKey]: e.target.value }))}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && dirtyCount > 0 && !saving) {
                    e.preventDefault();
                    void saveAllDirty();
                  }
                }}
                className={`${field.inputWidth ?? "w-32"} shrink-0 font-mono text-xs`}
              />
            </div>
          );
        })}
      </div>
    );
  };

  const notifActive = notifications.enabled && notifications.env.permission === "granted";
  const sandboxEnvCurrent = data?.values.sandboxEnvPassthrough ?? "";
  const sandboxEnvDraft = drafts.sandbox_env ?? sandboxEnvCurrent;

  return (
    <CanvasPage
      ariaLabel="Settings"
      onBack={onBack}
      status={status}
      configPath={data?.configPath}
      headerAction={
        <Button
          size="sm"
          variant="outline"
          isDisabled={saving}
          onPress={() => void saveSingleKey("reset")}
        >
          <RotateCcw aria-hidden="true" className="size-3.5" />
          Reset to defaults
        </Button>
      }
    >
      <div>
        <h1 className="text-lg font-semibold tracking-tight text-foreground">Settings</h1>
        <p className="mt-0.5 text-xs text-muted">
          Alerts, bot defaults, session compaction, and sandbox controls.
        </p>
        <p className="mt-1 text-xs text-muted/80">
          App v{import.meta.env.VITE_APP_VERSION || "0.1.0"} (build{" "}
          {import.meta.env.VITE_APP_BUILD || "dev"})
          {serverVersion ? ` · Server v${serverVersion}` : ""}
        </p>
      </div>

      <div
        role="tablist"
        aria-label="Settings sections"
        className="flex flex-wrap gap-1 rounded-xl border border-border bg-surface/40 p-1"
      >
        {SETTINGS_TABS.map((tab) => {
          const selected = activeTab === tab.id;
          return (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={selected}
              onClick={() => setActiveTab(tab.id)}
              className={`rounded-lg px-3 py-1.5 text-xs font-medium transition-colors ${
                selected
                  ? "bg-accent text-white shadow-xs"
                  : "text-muted hover:text-foreground"
              }`}
            >
              {tab.label}
            </button>
          );
        })}
      </div>

      {activeTab === "general" ? (
        <div role="tabpanel" aria-label="General & Alerts" className="space-y-4">
          <ServerUrlCard />
          <BiometricLockCard />
          <section
            aria-label="Notifications"
            className="rounded-xl border border-border bg-surface/50 px-4 py-3"
          >
            <div className="mb-3 flex items-center gap-1.5 border-b border-separator pb-2">
              <Bell aria-hidden="true" className="size-3.5 text-muted" />
              <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
                Notifications
              </h2>
            </div>
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div className="min-w-0">
                <p className="text-sm font-medium text-foreground">
                  {isNative ? "App notifications" : "Browser notifications"}
                </p>
                <p className="mt-0.5 text-xs text-muted">
                  {notificationStatusText(notifications, isNative)}
                </p>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                {notifications.env.supported && notifActive ? (
                  <Button
                    size="sm"
                    variant="outline"
                    onPress={() => {
                      void notifications.sendTest().then((ok) => {
                        setStatus(
                          ok
                            ? { ok: true, text: "Test notification sent." }
                            : { ok: false, text: "Could not send test notification." },
                        );
                      });
                    }}
                  >
                    Test
                  </Button>
                ) : null}
                <Button
                  size="sm"
                  variant={notifActive ? "outline" : "primary"}
                  isDisabled={!notifications.env.supported || notifications.env.permission === "denied"}
                  onPress={() => void notifications.setEnabled(!notifActive)}
                >
                  {notifActive ? "Disable" : "Enable"}
                </Button>
              </div>
            </div>
            <div className={`mt-3 space-y-2 border-t border-separator pt-3 ${notifActive ? "" : "opacity-60"}`}>
              {NOTIFICATION_CATEGORIES.map((item) => {
                const checked = notifications.prefs[item.key];
                return (
                  <div
                    key={item.key}
                    className="flex items-center justify-between gap-3 py-1"
                  >
                    <div className="min-w-0">
                      <p className="text-sm font-medium text-foreground">{item.label}</p>
                      <p className="text-xs text-muted">{item.description}</p>
                    </div>
                    <button
                      type="button"
                      role="switch"
                      aria-checked={checked}
                      aria-label={item.label}
                      disabled={!notifications.env.supported || notifications.env.permission === "denied"}
                      onClick={() => notifications.setCategoryEnabled(item.key, !checked)}
                      className={`shrink-0 rounded-full px-3 py-1 text-xs font-medium transition-colors ${
                        checked ? "bg-accent text-white" : "border border-border text-foreground"
                      } disabled:opacity-50`}
                    >
                      {checked ? "On" : "Off"}
                    </button>
                  </div>
                );
              })}
            </div>
            {isNative && batteryUnrestricted === false ? (
              <div className="mt-3 flex flex-col gap-2 border-t border-separator pt-3 sm:flex-row sm:items-center sm:justify-between">
                <div className="min-w-0">
                  <p className="text-xs font-medium text-foreground">
                    Battery optimization is restricting background alerts
                  </p>
                  <p className="text-xs text-muted">
                    Allow unrestricted background usage so lock-screen notifications arrive on time.
                  </p>
                </div>
                <Button
                  size="sm"
                  variant="outline"
                  onPress={() => void NativePlugin.requestIgnoreBatteryOptimizations()}
                >
                  Allow unrestricted
                </Button>
              </div>
            ) : null}
          </section>
        </div>
      ) : null}

      {loading && !data && activeTab !== "general" ? (
        <p className="text-sm text-muted">Loading settings…</p>
      ) : null}

      {data && activeTab === "bots" ? (
        <div role="tabpanel" aria-label="Bots" className="space-y-4">
          <section
            aria-label="Bot Defaults"
            className="rounded-xl border border-border bg-surface/50 px-4 py-3"
          >
            <div className="mb-3 flex items-center gap-1.5 border-b border-separator pb-2">
              <Bot aria-hidden="true" className="size-3.5 text-muted" />
              <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
                Bot Defaults
              </h2>
            </div>
            {renderFieldRows(BOT_GOVERNANCE_FIELDS)}
          </section>
        </div>
      ) : null}

      {data && activeTab === "session" ? (
        <div role="tabpanel" aria-label="Session & Context" className="space-y-4">
          <section
            aria-label="Session and Compaction"
            className="rounded-xl border border-border bg-surface/50 px-4 py-3"
          >
            <div className="mb-3 flex items-center gap-1.5 border-b border-separator pb-2">
              <MessageSquare aria-hidden="true" className="size-3.5 text-muted" />
              <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
                Session &amp; Compaction
              </h2>
            </div>
            <div className="mb-2 flex items-center justify-between gap-3 border-b border-separator pb-3">
              <div className="min-w-0">
                <p className="text-sm font-medium text-foreground">Auto-rename web sessions</p>
                <p className="text-xs text-muted">
                  Automatically generate a title after the first turn using the same model as the Global Turn Watcher. Skipped if you rename the session manually.
                </p>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={data.values.autoRenameEnabled ?? true}
                aria-label="Auto-rename web sessions"
                disabled={saving}
                onClick={() =>
                  void saveSingleKey(
                    "auto_rename_enabled",
                    (data.values.autoRenameEnabled ?? true) ? "off" : "on",
                  )
                }
                className={`shrink-0 rounded-full px-3 py-1 text-xs font-medium transition-colors ${
                  (data.values.autoRenameEnabled ?? true)
                    ? "bg-accent text-white"
                    : "border border-border text-foreground"
                } disabled:opacity-50`}
              >
                {(data.values.autoRenameEnabled ?? true) ? "On" : "Off"}
              </button>
            </div>
            {renderFieldRows(SESSION_FIELDS)}
          </section>
        </div>
      ) : null}

      {activeTab === "advanced" ? (
        <div role="tabpanel" aria-label="Advanced" className="space-y-4">
          {data ? (
            <>
              <TurnWatcherSettingsCard
                provider={drafts.watcher_provider ?? data.values.watcherProvider ?? ""}
                model={drafts.watcher_model ?? data.values.watcherModel ?? ""}
                antistallDefault={data.values.watcherAntistallDefault ?? false}
                delaySeconds={drafts.watcher_delay_seconds ?? String(data.values.watcherDelaySeconds ?? 30)}
                defaultMaxTurns={drafts.watcher_default_max_turns ?? String(data.values.watcherDefaultMaxTurns ?? 10)}
                antistallMaxNudges={drafts.watcher_antistall_max_nudges ?? String(data.values.watcherAntistallMaxNudges ?? 3)}
                defaultPrompt={drafts.watcher_default_prompt ?? data.values.watcherDefaultPrompt ?? ""}
                saving={saving}
                onChangeDraft={(key, value) => setDrafts((prev) => ({ ...prev, [key]: value }))}
                onToggleAntistall={() =>
                  void saveSingleKey(
                    "watcher_antistall_default",
                    (data.values.watcherAntistallDefault ?? false) ? "off" : "on",
                  )
                }
              />

              <section
                aria-label="Security and Sandbox"
                className="rounded-xl border border-border bg-surface/50 px-4 py-3"
              >
                <div className="mb-3 flex items-center gap-1.5 border-b border-separator pb-2">
                  <Shield aria-hidden="true" className="size-3.5 text-muted" />
                  <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
                    Security &amp; Sandbox
                  </h2>
                </div>
                <div className="divide-y divide-separator">
                  <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                    <div className="min-w-0">
                      <p className="text-sm font-medium text-foreground">Sandbox env passthrough</p>
                      <p className="text-xs text-muted">
                        Comma-separated env var names forwarded to sandboxed kernels. Host secrets are always blocked.
                      </p>
                    </div>
                    <Input
                      aria-label="Sandbox env passthrough"
                      value={sandboxEnvDraft}
                      placeholder="MY_VAR, OTHER_VAR"
                      onChange={(e) => setDrafts((prev) => ({ ...prev, sandbox_env: e.target.value }))}
                      className="w-48 shrink-0 font-mono text-xs"
                    />
                  </div>
                </div>
              </section>
            </>
          ) : null}

        </div>
      ) : null}

      {data && dirtyCount > 0 ? (
        <div
          role="region"
          aria-label="Unsaved settings bar"
          className="sticky bottom-3 flex items-center justify-between gap-3 rounded-xl border border-accent/40 bg-surface px-4 py-2.5 shadow-md"
        >
          <p className="text-xs font-medium text-foreground">
            {dirtyCount} unsaved {dirtyCount === 1 ? "change" : "changes"}
          </p>
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              variant="outline"
              isDisabled={saving}
              onPress={() => {
                applyPayloadValues(data.values, data.defaults);
                setStatus(null);
              }}
            >
              Discard
            </Button>
            <Button
              size="sm"
              variant="primary"
              isDisabled={saving}
              onPress={() => void saveAllDirty()}
            >
              <Save aria-hidden="true" className="size-3.5" />
              Save changes
            </Button>
          </div>
        </div>
      ) : null}
    </CanvasPage>
  );
}
