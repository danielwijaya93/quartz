import * as React from "react";
import type { ServerMessage } from "../protocol";
import { authFetch } from "./authToken";
import { checkNativePermission, clearNativeNotifications, ensureNativeChannel } from "./nativeNotifications";
import { apiUrl, isNativeCapacitor } from "./serverUrl";
import {
  compactNotificationBody,
  detectNotificationEnvironment,
  formatNotificationForFrame,
  readNotificationsEnabled,
  registerNotificationServiceWorker,
  requestNotificationPermission,
  sendWebPushTest,
  setDocumentUnreadBadge,
  shouldNotifyInBackground,
  showBrowserNotification,
  subscribeToWebPush,
  unsubscribeFromWebPush,
  writeNotificationsEnabled,
  type BrowserNotificationPayload,
  type NotificationEnvironment,
} from "./notifications";

export interface NotificationsController {
  enabled: boolean;
  env: NotificationEnvironment;
  notifyFromFrame: (message: ServerMessage) => void;
  setEnabled: (next: boolean) => Promise<void>;
  applySyncedEnabled: (next: boolean) => Promise<void>;
  clearDelivered: () => void;
  sendTest: () => Promise<boolean>;
}

export interface UseNotificationsOptions {
  onNotificationsRead?: () => void;
}

function persistNotificationsToServer(enabled: boolean): void {
  void authFetch(apiUrl("/api/settings"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key: "notifications", value: enabled ? "on" : "off" }),
  }).catch(() => {
    /* Offline or test environment: localStorage still holds the choice. */
  });
}

export function useNotifications(options?: UseNotificationsOptions): NotificationsController {
  const [enabled, setEnabledState] = React.useState<boolean>(() => readNotificationsEnabled());
  const [env, setEnv] = React.useState<NotificationEnvironment>(() =>
    detectNotificationEnvironment(),
  );
  const unreadRef = React.useRef(0);
  const enabledRef = React.useRef(enabled);
  const pushEndpointRef = React.useRef<string | null>(null);
  const notifiedKeysRef = React.useRef<Set<string>>(new Set());
  const activeSeededRef = React.useRef(false);
  const onReadRef = React.useRef(options?.onNotificationsRead);
  onReadRef.current = options?.onNotificationsRead;
  enabledRef.current = enabled;

  const clearDelivered = React.useCallback(() => {
    unreadRef.current = 0;
    setDocumentUnreadBadge(0);
    if (isNativeCapacitor()) void clearNativeNotifications();
  }, []);

  const deliverPayload = React.useCallback((payload: BrowserNotificationPayload) => {
    unreadRef.current += 1;
    setDocumentUnreadBadge(unreadRef.current);
    // When Web Push is active, sw.js delivers the OS notification via the push
    // channel (which also wakes the worker when the phone is backgrounded). In a
    // native shell there is no service worker at all, so the local notification is
    // the delivery -- and it is not suppressed by a stale push endpoint.
    if (isNativeCapacitor() || !pushEndpointRef.current) {
      void showBrowserNotification(payload);
    }
  }, []);

  React.useEffect(() => {
    void registerNotificationServiceWorker();
    const initialEnv = detectNotificationEnvironment();
    setEnv(initialEnv);
    if (isNativeCapacitor()) {
      void ensureNativeChannel();
      void checkNativePermission().then(async (perm) => {
        if (enabledRef.current && perm === "default") {
          await requestNotificationPermission();
        }
        setEnv(detectNotificationEnvironment());
      });
    } else if (enabledRef.current && initialEnv.supported && initialEnv.permission === "granted") {
      void subscribeToWebPush().then((endpoint) => {
        pushEndpointRef.current = endpoint;
      });
    }

    const clearBadge = () => {
      if (document.visibilityState === "visible" || document.hasFocus()) {
        const hadUnread = unreadRef.current > 0;
        clearDelivered();
        if (hadUnread) {
          onReadRef.current?.();
        }
      }
      setEnv(detectNotificationEnvironment());
    };

    window.addEventListener("focus", clearBadge);
    document.addEventListener("visibilitychange", clearBadge);
    return () => {
      window.removeEventListener("focus", clearBadge);
      document.removeEventListener("visibilitychange", clearBadge);
      setDocumentUnreadBadge(0);
    };
  }, [clearDelivered]);

  const notifyFromFrame = React.useCallback(
    (message: ServerMessage) => {
      if (message.type === "notifications_cleared") {
        clearDelivered();
        return;
      }
      if (!enabledRef.current) return;

      if (message.type === "active_sessions_state") {
        // Seed initial state when the app is visible so opening the app does not
        // re-notify for old unread sessions; notify when backgrounded/locked.
        const inBackground = shouldNotifyInBackground();
        if (!activeSeededRef.current && !inBackground) {
          activeSeededRef.current = true;
          for (const item of message.items) {
            if (!item.read) {
              notifiedKeysRef.current.add(`${item.sessionId}:${item.kind}:${item.updatedAt}`);
            }
          }
          return;
        }
        activeSeededRef.current = true;
        if (!isNativeCapacitor() && !inBackground) return;
        for (const item of message.items) {
          if (item.read) continue;
          if (item.kind !== "done" && item.kind !== "error" && item.kind !== "permission") continue;
          const key = `${item.sessionId}:${item.kind}:${item.updatedAt}`;
          if (notifiedKeysRef.current.has(key)) continue;
          notifiedKeysRef.current.add(key);
          // Only notify via active_sessions_state when backgrounded so foreground
          // native users are not double-notified alongside turn_end/permission_request.
          if (!inBackground) continue;
          const label = item.title || "Quartz";
          const body =
            item.kind === "permission"
              ? "Approval needed."
              : compactNotificationBody(item.preview || "") ||
                (item.kind === "error" ? "Turn failed." : "Turn finished.");
          deliverPayload({
            title: item.kind === "permission" ? `${label}: Approval needed` : label,
            body,
            tag: `quartz-session-${item.sessionId}`,
          });
        }
        return;
      }

      if (message.type === "session_state" && message.pendingPermission) {
        if (!shouldNotifyInBackground()) return;
        const perm = message.pendingPermission;
        const key = `perm:${perm.requestId}`;
        if (notifiedKeysRef.current.has(key)) return;
        notifiedKeysRef.current.add(key);
        deliverPayload({
          title: "Quartz: Approval needed",
          body: compactNotificationBody(`Allow ${perm.command || perm.tool || "action"}?`),
          tag: `quartz-permission-${perm.requestId}`,
        });
        return;
      }

      const payload = formatNotificationForFrame(message);
      if (!payload) return;
      if (!isNativeCapacitor() && !shouldNotifyInBackground()) return;
      if (message.type === "permission_request") {
        notifiedKeysRef.current.add(`perm:${message.requestId}`);
      }
      deliverPayload(payload);
    },
    [clearDelivered, deliverPayload],
  );

  const applySyncedEnabled = React.useCallback(async (next: boolean) => {
    if (!next) {
      writeNotificationsEnabled(false);
      setEnabledState(false);
      pushEndpointRef.current = null;
      await unsubscribeFromWebPush();
      return;
    }
    const refreshed = detectNotificationEnvironment();
    setEnv(refreshed);
    writeNotificationsEnabled(true);
    setEnabledState(true);
    if (isNativeCapacitor()) {
      pushEndpointRef.current = null;
      return;
    }
    if (refreshed.supported && refreshed.permission === "granted" && !pushEndpointRef.current) {
      pushEndpointRef.current = await subscribeToWebPush();
    }
  }, []);

  const setEnabled = React.useCallback(async (next: boolean) => {
    if (!next) {
      writeNotificationsEnabled(false);
      setEnabledState(false);
      pushEndpointRef.current = null;
      persistNotificationsToServer(false);
      await unsubscribeFromWebPush();
      return;
    }
    const currentEnv = detectNotificationEnvironment();
    if (currentEnv.supported && currentEnv.permission === "default") {
      await requestNotificationPermission();
    }
    const refreshed = detectNotificationEnvironment();
    setEnv(refreshed);
    writeNotificationsEnabled(true);
    setEnabledState(true);
    persistNotificationsToServer(true);
    if (isNativeCapacitor()) {
      // Native delivery is a local notification; there is no push subscription to make.
      pushEndpointRef.current = null;
      return;
    }
    if (refreshed.supported && refreshed.permission === "granted") {
      pushEndpointRef.current = await subscribeToWebPush();
    }
  }, []);

  const sendTest = React.useCallback(async () => {
    const currentEnv = detectNotificationEnvironment();
    if (currentEnv.supported && currentEnv.permission === "default") {
      await requestNotificationPermission();
    }
    const refreshed = detectNotificationEnvironment();
    setEnv(refreshed);
    if (!refreshed.supported || refreshed.permission !== "granted") {
      return false;
    }
    if (isNativeCapacitor()) {
      return showBrowserNotification({
        title: "Quartz",
        body: "Notifications are working on this device.",
        tag: "quartz-test",
      });
    }
    if (!pushEndpointRef.current) {
      pushEndpointRef.current = await subscribeToWebPush();
    }
    if (pushEndpointRef.current) {
      const pushed = await sendWebPushTest(pushEndpointRef.current);
      if (pushed) return true;
    }
    return showBrowserNotification({
      title: "Quartz",
      body: "Browser notifications are working.",
      tag: "quartz-test",
    });
  }, []);

  return {
    enabled,
    env,
    notifyFromFrame,
    setEnabled,
    applySyncedEnabled,
    clearDelivered,
    sendTest,
  };
}
