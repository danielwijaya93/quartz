import * as React from "react";
import { ServerUrlCard } from "./ServerUrlCard";
import { apiUrl, isNativeCapacitor } from "../lib/serverUrl";
import { NativePlugin } from "../lib/nativeBridge";
import { BiometricLockCard } from "./BiometricLockCard";
import { authFetch } from "../lib/authToken";
import { Button, Input } from "@heroui/react";
import { Bell, RotateCcw } from "lucide-react";
import { useNotifications, type NotificationsController } from "../lib/useNotifications";
import { CanvasPage } from "./CanvasPage";

interface SettingsValues {
  reserve: number;
  keep: number;
  progress: number;
  bg: number;
  idle?: number;
}

interface SettingsPayload {
  values: SettingsValues;
  defaults: SettingsValues;
  configPath: string;
}

const SETTING_FIELDS: readonly {
  key: keyof SettingsValues;
  label: string;
  hint: (def: number) => string;
}[] = [
  { key: "reserve", label: "Reserve tokens", hint: (def) => `Default ${def.toLocaleString()}` },
  { key: "keep", label: "Keep recent tokens", hint: (def) => `Default ${def.toLocaleString()}` },
  { key: "progress", label: "Progress update interval (turns)", hint: (def) => `Default ${def}, 0 disables` },
  { key: "bg", label: "Background check interval (seconds)", hint: (def) => `Default ${def}s, 0 disables` },
  { key: "idle", label: "Idle session check interval (hours)", hint: (def) => `Default ${def}h, 0 disables` },
];

function draftsFromValues(values: SettingsValues, fallbackIdle?: number): Record<string, string> {
  return {
    reserve: String(values.reserve),
    keep: String(values.keep),
    progress: String(values.progress),
    bg: String(values.bg),
    idle: String(values.idle ?? fallbackIdle ?? 2),
  };
}

function notificationStatusText(notifications: NotificationsController, isNative: boolean): string {
  const { env, enabled } = notifications;
  if (env.needsHttps) return "Requires HTTPS (for example via Tailscale Serve) or localhost.";
  if (env.needsHomeScreen) {
    return "On iPhone or iPad, tap Share → Add to Home Screen, then open Quartz from your Home Screen.";
  }
  if (env.permission === "denied") {
    return isNative
      ? "Blocked in Android app settings. Allow notifications for Quartz to enable them."
      : "Blocked in browser settings. Allow notifications for this site to enable them.";
  }
  if (!env.supported) return "Not supported in this browser.";
  if (env.permission === "granted" && enabled) {
    return isNative
      ? "Enabled — alerts when a turn finishes, needs approval, or a background job ends."
      : "Enabled — alerts when a turn finishes, needs approval, or a background job ends while inactive.";
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
  const [data, setData] = React.useState<SettingsPayload | null>(null);
  const [drafts, setDrafts] = React.useState<Record<string, string>>({});
  const [loading, setLoading] = React.useState(false);
  const [savingKey, setSavingKey] = React.useState<string | null>(null);
  const [status, setStatus] = React.useState<{ ok: boolean; text: string } | null>(null);
  const [serverVersion, setServerVersion] = React.useState<string | null>(null);
  const [batteryUnrestricted, setBatteryUnrestricted] = React.useState<boolean | null>(null);
  const isNative = isNativeCapacitor();

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
      setDrafts(draftsFromValues(payload.values, payload.defaults.idle));
    } catch {
      setStatus({ ok: false, text: "Could not load settings." });
    } finally {
      setLoading(false);
    }
  }, []);

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

  const saveKey = async (key: string, value?: string): Promise<void> => {
    setSavingKey(key);
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
        setDrafts(draftsFromValues(body.values, data?.defaults.idle));
      }
      setStatus({ ok: true, text: body.message || "Saved." });
    } catch {
      setStatus({ ok: false, text: "Request failed." });
    } finally {
      setSavingKey(null);
    }
  };

  const notifActive = notifications.enabled && notifications.env.permission === "granted";

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
          isDisabled={savingKey !== null}
          onPress={() => void saveKey("reset")}
        >
          <RotateCcw aria-hidden="true" className="size-3.5" />
          Reset to defaults
        </Button>
      }
    >
      <ServerUrlCard />
      <BiometricLockCard />

      <div>
        <h1 className="text-lg font-semibold tracking-tight text-foreground">Settings</h1>
        <p className="mt-0.5 text-xs text-muted">
          Session compaction and background check defaults.
        </p>
        <p className="mt-1 text-xs text-muted/80">
          App v{import.meta.env.VITE_APP_VERSION || "0.1.0"} (build{" "}
          {import.meta.env.VITE_APP_BUILD || "dev"})
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
                    onChange={(e) => setDrafts((prev) => ({ ...prev, [field.key]: e.target.value }))}
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
        aria-label={isNative ? "App notifications" : "Browser notifications"}
        className="rounded-xl border border-border bg-surface/50 px-4 py-3"
      >
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <div className="flex items-center gap-1.5">
              <Bell aria-hidden="true" className="size-3.5 text-muted" />
              <p className="text-sm font-medium text-foreground">
                {isNative ? "App notifications" : "Browser notifications"}
              </p>
            </div>
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
    </CanvasPage>
  );
}
