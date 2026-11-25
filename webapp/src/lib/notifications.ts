import { authFetch } from "./authToken";
import { apiUrl, isNativeCapacitor } from "./serverUrl";
import { requestNativePermission, showNativeNotification } from "./nativeNotifications";
import type { ServerMessage } from "../protocol";

export type NotificationPermissionState = "granted" | "denied" | "default" | "unsupported";

export interface NotificationEnvironment {
  supported: boolean;
  secureContext: boolean;
  isIos: boolean;
  isAndroid: boolean;
  isStandalone: boolean;
  needsHttps: boolean;
  needsHomeScreen: boolean;
  permission: NotificationPermissionState;
}

export type NotificationCategory = "turn" | "approval" | "background" | "error";

export interface NotificationPreferences {
  turn: boolean;
  approval: boolean;
  background: boolean;
  error: boolean;
}

export const DEFAULT_NOTIFICATION_PREFERENCES: NotificationPreferences = {
  turn: true,
  approval: true,
  background: true,
  error: true,
};

export interface BrowserNotificationPayload {
  title: string;
  body: string;
  tag: string;
}

const STORAGE_KEY = "quartz-web-notifications";
const PREFS_STORAGE_KEY = "quartz-web-notification-prefs";
const DEFAULT_TITLE = "Quartz";

export function readNotificationsEnabled(): boolean {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw === "false" || raw === "0") return false;
    return true;
  } catch {
    return true;
  }
}

export function writeNotificationsEnabled(enabled: boolean): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, enabled ? "true" : "false");
  } catch {
    /* Private mode or blocked storage. */
  }
}

export function readNotificationPreferences(): NotificationPreferences {
  try {
    const raw = window.localStorage.getItem(PREFS_STORAGE_KEY);
    if (!raw) return { ...DEFAULT_NOTIFICATION_PREFERENCES };
    const parsed = JSON.parse(raw) as Partial<Record<NotificationCategory, unknown>>;
    if (!parsed || typeof parsed !== "object") return { ...DEFAULT_NOTIFICATION_PREFERENCES };
    return {
      turn: parsed.turn !== false,
      approval: parsed.approval !== false,
      background: parsed.background !== false,
      error: parsed.error !== false,
    };
  } catch {
    return { ...DEFAULT_NOTIFICATION_PREFERENCES };
  }
}

export function writeNotificationPreferences(prefs: NotificationPreferences): void {
  try {
    window.localStorage.setItem(PREFS_STORAGE_KEY, JSON.stringify(prefs));
  } catch {
    /* Private mode or blocked storage. */
  }
}

export function detectNotificationEnvironment(): NotificationEnvironment {
  const nav = typeof navigator !== "undefined" ? navigator : undefined;
  const ua = nav?.userAgent ?? "";
  const isIos =
    /iPad|iPhone|iPod/.test(ua) ||
    (nav?.platform === "MacIntel" && (nav?.maxTouchPoints ?? 0) > 1);
  const isAndroid = /Android/i.test(ua);
  const isStandalone =
    Boolean((nav as { standalone?: boolean } | undefined)?.standalone) ||
    (typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia("(display-mode: standalone)").matches);

  const secureContext = typeof window !== "undefined" ? Boolean(window.isSecureContext) : false;
  const hasApi = typeof window !== "undefined" && typeof window.Notification !== "undefined";
  const permission: NotificationPermissionState = hasApi
    ? (window.Notification.permission as NotificationPermissionState)
    : "unsupported";

  // A packaged shell reports support from the native plugin, not from the web API it
  // does not have. Without this the settings toggle stays hidden on Android and the
  // app quietly never notifies.
  if (isNativeCapacitor()) {
    return {
      supported: true,
      secureContext: true,
      isIos: false,
      isAndroid: true,
      isStandalone: true,
      needsHttps: false,
      needsHomeScreen: false,
      permission: nativePermissionHint(),
    };
  }

  return {
    supported: hasApi && secureContext,
    secureContext,
    isIos,
    isAndroid,
    isStandalone,
    needsHttps: !secureContext,
    needsHomeScreen: isIos && !isStandalone,
    permission,
  };
}

/**
 * What to show before the async native check runs.
 *
 * The real answer needs an `await` on the plugin, and this function is synchronous
 * because it is called during render. "granted" is the honest default: Android 13+
 * asks once on first use, and everything below 13 grants at install.
 */
function nativePermissionHint(): NotificationPermissionState {
  try {
    const cached = window.localStorage.getItem("quartz:native-notif-perm");
    if (cached === "granted" || cached === "denied" || cached === "default") {
      return cached;
    }
    return "default";
  } catch {
    return "default";
  }
}

let swPromise: Promise<ServiceWorkerRegistration | null> | null = null;

export function registerNotificationServiceWorker(): Promise<ServiceWorkerRegistration | null> {
  if (
    typeof window === "undefined" ||
    !window.isSecureContext ||
    typeof navigator === "undefined" ||
    !("serviceWorker" in navigator)
  ) {
    return Promise.resolve(null);
  }
  if (!swPromise) {
    swPromise = navigator.serviceWorker.register("/sw.js").catch(() => null);
  }
  return swPromise;
}

async function awaitServiceWorkerReady(timeoutMs = 2000): Promise<ServiceWorkerRegistration | null> {
  const reg = await registerNotificationServiceWorker();
  if (!reg || typeof navigator === "undefined" || !("serviceWorker" in navigator)) {
    return reg;
  }
  const readyPromise = navigator.serviceWorker.ready;
  if (!readyPromise) return reg;
  try {
    return await Promise.race([
      readyPromise,
      new Promise<ServiceWorkerRegistration>((resolve) => {
        setTimeout(() => resolve(reg), timeoutMs);
      }),
    ]);
  } catch {
    return reg;
  }
}

export function urlBase64ToUint8Array(base64String: string): Uint8Array<ArrayBuffer> {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = window.atob(base64);
  const buffer = new ArrayBuffer(raw.length);
  const output = new Uint8Array(buffer);
  for (let i = 0; i < raw.length; i += 1) {
    output[i] = raw.charCodeAt(i);
  }
  return output;
}

export async function subscribeToWebPush(): Promise<string | null> {
  if (
    typeof window === "undefined" ||
    !window.isSecureContext ||
    typeof navigator === "undefined" ||
    !("serviceWorker" in navigator) ||
    !("PushManager" in window) ||
    typeof window.Notification === "undefined" ||
    window.Notification.permission !== "granted"
  ) {
    return null;
  }

  try {
    const reg = await awaitServiceWorkerReady();
    if (!reg || !reg.pushManager) return null;

    const keyRes = await authFetch(apiUrl("/api/push/vapid-key"));
    if (!keyRes.ok) return null;
    const keyData = (await keyRes.json()) as { publicKey?: string };
    const publicKey = keyData.publicKey;
    if (!publicKey) return null;

    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(publicKey),
      });
    }

    const subJson = sub.toJSON();
    const res = await authFetch(apiUrl("/api/push/subscribe"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(subJson),
    });
    if (!res.ok) return null;
    return sub.endpoint;
  } catch {
    return null;
  }
}

export async function unsubscribeFromWebPush(): Promise<void> {
  if (
    typeof window === "undefined" ||
    !window.isSecureContext ||
    typeof navigator === "undefined" ||
    !("serviceWorker" in navigator)
  ) {
    return;
  }
  try {
    const reg = await awaitServiceWorkerReady();
    if (!reg || !reg.pushManager) return;
    const sub = await reg.pushManager.getSubscription();
    if (!sub) return;
    await authFetch(apiUrl("/api/push/unsubscribe"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ endpoint: sub.endpoint }),
    }).catch(() => undefined);
    await sub.unsubscribe().catch(() => undefined);
  } catch {
    /* ignore */
  }
}

export async function sendWebPushTest(endpoint?: string | null): Promise<boolean> {
  try {
    const res = await authFetch(apiUrl("/api/push/test"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(endpoint ? { endpoint } : {}),
    });
    if (!res.ok) return false;
    const data = (await res.json()) as { ok?: boolean };
    return Boolean(data.ok);
  } catch {
    return false;
  }
}

export async function requestNotificationPermission(): Promise<NotificationPermissionState> {
  // Android asks through its own runtime permission dialog, not the web API.
  if (isNativeCapacitor()) {
    const native = await requestNativePermission();
    if (native === "granted") return "granted";
    if (native === "denied") return "denied";
    if (native === "unsupported") return "unsupported";
    return "default";
  }
  if (typeof window === "undefined" || typeof window.Notification === "undefined") {
    return "unsupported";
  }
  try {
    const result = await window.Notification.requestPermission();
    return result as NotificationPermissionState;
  } catch {
    return (window.Notification.permission as NotificationPermissionState) ?? "default";
  }
}

export function shouldNotifyInBackground(): boolean {
  if (typeof document === "undefined") return false;
  if (document.visibilityState !== "visible") return true;
  if (typeof document.hasFocus === "function") {
    return !document.hasFocus();
  }
  return false;
}

export function setDocumentUnreadBadge(count: number): void {
  if (typeof document === "undefined") return;
  document.title = count > 0 ? `(${count}) ${DEFAULT_TITLE}` : DEFAULT_TITLE;
}

const MAX_NOTIFICATION_BODY_LENGTH = 120;

export function compactNotificationBody(
  text: string,
  maxLength = MAX_NOTIFICATION_BODY_LENGTH,
): string {
  const collapsed = text.replace(/\s+/g, " ").trim();
  if (collapsed.length <= maxLength) return collapsed;
  return `${collapsed.slice(0, Math.max(0, maxLength - 3)).trimEnd()}...`;
}

export function formatNotificationForFrame(
  message: ServerMessage,
  prefs: NotificationPreferences = DEFAULT_NOTIFICATION_PREFERENCES,
): BrowserNotificationPayload | null {
  switch (message.type) {
    case "turn_end": {
      if (!prefs.turn) return null;
      if (message.status === "ok") {
        const secs = (message.elapsedMs / 1000).toFixed(1);
        return {
          title: "Quartz: Turn finished",
          body: `Completed in ${secs}s.`,
          tag: "quartz-turn",
        };
      }
      if (message.status === "error") {
        const err = compactNotificationBody(message.error || "");
        return {
          title: "Quartz: Turn failed",
          body: err || "An error occurred during the turn.",
          tag: "quartz-turn",
        };
      }
      return null;
    }
    case "permission_request":
      if (!prefs.approval) return null;
      return {
        title: "Quartz: Approval needed",
        body: compactNotificationBody(`Allow ${message.command || message.tool || "action"}?`),
        tag: `quartz-permission-${message.requestId}`,
      };
    case "bash_notice":
      if (!prefs.background) return null;
      return {
        title: "Quartz: Background job finished",
        body: compactNotificationBody(message.text || "") || "A background job finished.",
        tag: "quartz-bash",
      };
    case "error":
      if (!prefs.error) return null;
      return {
        title: "Quartz: Error",
        body: compactNotificationBody(message.message || "") || "An error occurred.",
        tag: "quartz-error",
      };
    default:
      return null;
  }
}

export async function showBrowserNotification(
  payload: BrowserNotificationPayload,
): Promise<boolean> {
  // A packaged app has no service worker and no `window.Notification`: without this
  // branch the native build would silently notify nobody, which is exactly the
  // regression that made the APK worse than the browser.
  if (isNativeCapacitor()) return showNativeNotification(payload);

  if (
    typeof window === "undefined" ||
    !window.isSecureContext ||
    typeof window.Notification === "undefined" ||
    window.Notification.permission !== "granted"
  ) {
    return false;
  }

  const options: NotificationOptions = {
    body: payload.body,
    tag: payload.tag,
    icon: "/icon-192.png",
    badge: "/icon-192.png",
  };

  // Prefer ServiceWorkerRegistration.showNotification: required on Android Chrome
  // and iOS Home Screen PWAs, and works cleanly on desktop browsers too.
  if (typeof navigator !== "undefined" && "serviceWorker" in navigator) {
    try {
      const reg = await awaitServiceWorkerReady();
      if (reg && typeof reg.showNotification === "function") {
        await reg.showNotification(payload.title, options);
        return true;
      }
    } catch {
      /* Fall back to window.Notification below. */
    }
  }

  try {
    const n = new window.Notification(payload.title, options);
    n.onclick = () => {
      try {
        window.focus();
      } catch {
        /* ignore */
      }
      n.close();
    };
    return true;
  } catch {
    return false;
  }
}