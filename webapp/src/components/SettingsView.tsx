import * as React from "react";
import { ServerUrlCard } from "./ServerUrlCard";
import { BiometricLockCard } from "./BiometricLockCard";
import { apiUrl } from "../lib/serverUrl";
import { Button, Input } from "@heroui/react";
import { ArrowLeft, Bell, RotateCcw } from "lucide-react";
import {
  useNotifications,
  type NotificationsController,
} from "../lib/useNotifications";

interface SettingsPayload {
  values: {
    reserve: number;
    keep: number;
    progress: number;
    bg: number;
    idle?: number;
  };
  defaults: {
    reserve: number;
    keep: number;
    progress: number;
    bg: number;
    idle?: number;
  };
  configPath: string;
}

const SETTING_FIELDS: readonly {
  key: "reserve" | "keep" | "progress" | "bg" | "idle";
  label: string;
  hint: (def: number) => string;
}[] = [
  {
    key: "reserve",
    label: "Reserve tokens",
    hint: (def) => `Default ${def.toLocaleString()}`,
  },
  {
    key: "keep",
    label: "Keep recent tokens",
    hint: (def) => `Default ${def.toLocaleString()}`,
  },
  {
    key: "progress",
    label: "Progress update interval (turns)",
    hint: (def) => `Default ${def}, 0 disables`,
  },
  {
    key: "bg",
    label: "Background check interval (seconds)",
    hint: (def) => `Default ${def}s, 0 disables`,
  },
  {
    key: "idle",
    label: "Idle session check interval (hours)",
    hint: (def) => `Default ${def}h, 0 disables`,
  },
];

/**
 * In-canvas settings view: replaces the transcript column with a calm, native
 * page and a `Back to chat` button—no full-screen dimmed backdrop.
 */
export function SettingsView({
  onBack,
  notifications: externalNotifications,
}: {
  onBack: () => void;
  notifications?: NotificationsController;
}): React.ReactElement {
  const fallbackNotifications = useNotifications();
  const notifications = externalNotifications ?? fallbackNotifications;
  const [data, setData] = React.useState<SettingsPayload | null>(null);
  const [drafts, setDrafts] = React.useState<Record<string, string>>({});
  const [loading, setLoading] = React.useState(false);
  const [savingKey, setSavingKey] = React.useState<string | null>(null);
  const [status, setStatus] = React.useState<{ ok: boolean; text: string } | null>(null);
  const [serverVersion, setServerVersion] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    setLoading(true);
    setStatus(null);
    try {
      const [resp, healthResp] = await Promise.all([
        fetch(apiUrl("/api/settings")),
        fetch(apiUrl("/api/health")).catch(() => null),
      ]);
      if (healthResp?.ok) {
        const health = (await healthResp.json()) as { version?: unknown };
        if (typeof health.version === "string" && health.version) {
          setServerVersion(health.version);
        }
      }
      if (!resp.ok) throw new Error("Failed to load settings");
      const payload = (await resp.json()) as SettingsPayload;
      setData(payload);
      setDrafts({
        reserve: String(payload.values.reserve),
        keep: String(payload.values.keep),
        progress: String(payload.values.progress),
        bg: String(payload.values.bg),
        idle: String(payload.values.idle ?? payload.defaults.idle ?? 2),
      });
    } catch {
      setStatus({ ok: false, text: "Could not load settings." });
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    void load();
  }, [load]);

  const saveKey = async (key: string, value?: string): Promise<void> => {
    setSavingKey(key);
    setStatus(null);
    try {
      const resp = await fetch(apiUrl("/api/settings"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(key === "reset" ? { key: "reset" } : { key, value }),
      });
      const body = (await resp.json()) as {
        ok?: boolean;
        error?: string;
        message?: string;
        values?: SettingsPayload["values"];
      };
      if (!resp.ok || !body.ok) {
        setStatus({ ok: false, text: body.error || "Failed to save setting." });
        return;
      }
      if (body.values) {
        setData((prev) => (prev ? { ...prev, values: body.values! } : prev));
        setDrafts({
          reserve: String(body.values.reserve),
          keep: String(body.values.keep),
          progress: String(body.values.progress),
          bg: String(body.values.bg),
          idle: String(body.values.idle ?? data?.defaults.idle ?? 2),
        });
      }
      setStatus({ ok: true, text: body.message || "Saved." });
    } catch {
      setStatus({ ok: false, text: "Request failed." });
    } finally {
      setSavingKey(null);
    }
  };

  return (
    <main
      aria-label="Settings"
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.stopPropagation();
          onBack();
        }
      }}
      className="min-h-0 flex-1 overflow-y-auto px-4 py-6 sm:px-5"
    >
      <div className="mx-auto w-full max-w-3xl space-y-5">
        <div className="flex items-center justify-between gap-3">
          <Button
            size="sm"
            variant="ghost"
            onPress={onBack}
            className="h-8 gap-1.5 px-2 text-muted hover:text-foreground"
          >
            <ArrowLeft aria-hidden="true" className="size-4" />
            Back to chat
          </Button>
          <Button
            size="sm"
            variant="outline"
            isDisabled={savingKey !== null}
            onPress={() => void saveKey("reset")}
          >
            <RotateCcw aria-hidden="true" className="size-3.5" />
            Reset to defaults
          </Button>
        </div>

        <ServerUrlCard />
        <BiometricLockCard />

        <div>
          <h1 className="text-lg font-semibold tracking-tight text-foreground">Settings</h1>
          <p className="mt-0.5 text-xs text-muted">
            Session compaction and background check defaults.
          </p>
          <p className="mt-1 text-xs text-muted/80">
            App v{import.meta.env.VITE_APP_VERSION || "0.1.0"} (build {import.meta.env.VITE_APP_BUILD || "dev"})
            {serverVersion ? ` · Server v${serverVersion}` : ""}
          </p>
        </div>

        {loading && !data ? (
          <p className="text-sm text-muted">Loading settings…</p>
        ) : data ? (
          <div className="divide-y divide-separator rounded-xl border border-border bg-surface/50">
            {SETTING_FIELDS.map((field) => {
              const current = String(data.values[field.key] ?? data.defaults[field.key] ?? 0);
              const val = drafts[field.key] ?? current;
              const dirty = val.trim() !== current;
              return (
                <div
                  key={field.key}
                  className="flex flex-col gap-2 px-4 py-3 sm:flex-row sm:items-center sm:justify-between"
                >
                  <div className="min-w-0">
                    <p className="text-sm font-medium text-foreground">{field.label}</p>
                    <p className="text-xs text-muted">{field.hint(data.defaults[field.key] ?? 0)}</p>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <Input
                      aria-label={field.label}
                      value={val}
                      onChange={(e) =>
                        setDrafts((prev) => ({ ...prev, [field.key]: e.target.value }))
                      }
                      onKeyDown={(e) => {
                        if (e.key === "Enter") {
                          e.preventDefault();
                          void saveKey(field.key, val.trim());
                        }
                      }}
                      className="w-28 font-mono text-xs"
                    />
                    <Button
                      size="sm"
                      variant={dirty ? "primary" : "outline"}
                      isDisabled={savingKey !== null || val.trim() === ""}
                      onPress={() => void saveKey(field.key, val.trim())}
                    >
                      Save
                    </Button>
                  </div>
                </div>
              );
            })}
          </div>
        ) : null}

        <section
          aria-label="Browser notifications"
          className="rounded-xl border border-border bg-surface/50 px-4 py-3"
        >
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="min-w-0">
              <div className="flex items-center gap-1.5">
                <Bell aria-hidden="true" className="size-3.5 text-muted" />
                <p className="text-sm font-medium text-foreground">Browser notifications</p>
              </div>
              <p className="mt-0.5 text-xs text-muted">
                {notifications.env.needsHttps
                  ? "Requires HTTPS (for example via Tailscale Serve) or localhost."
                  : notifications.env.needsHomeScreen
                    ? "On iPhone or iPad, tap Share → Add to Home Screen, then open Quartz from your Home Screen."
                    : notifications.env.permission === "denied"
                      ? "Blocked in browser settings. Allow notifications for this site to enable them."
                      : !notifications.env.supported
                        ? "Not supported in this browser."
                        : notifications.env.permission === "granted" && notifications.enabled
                          ? "Enabled — alerts when a turn finishes, needs approval, or a background job ends while inactive."
                          : notifications.env.permission === "default"
                            ? "Allow notifications to get alerts when Quartz is in the background."
                            : "Paused for this browser."}
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              {notifications.env.supported &&
              notifications.env.permission === "granted" &&
              notifications.enabled ? (
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
                variant={
                  notifications.enabled && notifications.env.permission === "granted"
                    ? "outline"
                    : "primary"
                }
                isDisabled={
                  !notifications.env.supported || notifications.env.permission === "denied"
                }
                onPress={() => {
                  const next = !(
                    notifications.enabled && notifications.env.permission === "granted"
                  );
                  void notifications.setEnabled(next);
                }}
              >
                {notifications.enabled && notifications.env.permission === "granted"
                  ? "Disable"
                  : "Enable"}
              </Button>
            </div>
          </div>
        </section>

        {status ? (
          <p role="status" className={status.ok ? "text-xs text-success" : "text-xs text-danger"}>
            {status.text}
          </p>
        ) : null}
        {data ? <p className="text-xs text-muted">Stored in {data.configPath}</p> : null}
      </div>
    </main>
  );
}