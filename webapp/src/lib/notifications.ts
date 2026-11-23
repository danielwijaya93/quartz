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

export interface BrowserNotificationPayload {
  title: string;
  body: string;
  tag: string;
}

const STORAGE_KEY = "quartz-web-notifications";
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

export async function requestNotificationPermission(): Promise<NotificationPermissionState> {
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

export function formatNotificationForFrame(
  message: ServerMessage,
): BrowserNotificationPayload | null {
  switch (message.type) {
    case "turn_end": {
      if (message.status === "ok") {
        const secs = (message.elapsedMs / 1000).toFixed(1);
        return {
          title: "Quartz: Turn finished",
          body: `Completed in ${secs}s.`,
          tag: "quartz-turn",
        };
      }
      if (message.status === "error") {
        return {
          title: "Quartz: Turn failed",
          body: message.error || "An error occurred during the turn.",
          tag: "quartz-turn",
        };
      }
      return null;
    }
    case "permission_request":
      return {
        title: "Quartz: Approval needed",
        body: `Allow ${message.command}?`,
        tag: `quartz-permission-${message.requestId}`,
      };
    case "bash_notice":
      return {
        title: "Quartz: Background job finished",
        body: message.text,
        tag: "quartz-bash",
      };
    case "error":
      return {
        title: "Quartz: Error",
        body: message.message,
        tag: "quartz-error",
      };
    default:
      return null;
  }
}

export async function showBrowserNotification(
  payload: BrowserNotificationPayload,
): Promise<boolean> {
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
      const reg = await registerNotificationServiceWorker();
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
