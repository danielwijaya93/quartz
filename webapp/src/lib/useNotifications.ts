import * as React from "react";
import type { ServerMessage } from "../protocol";
import {
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
  type NotificationEnvironment,
} from "./notifications";

export interface NotificationsController {
  enabled: boolean;
  env: NotificationEnvironment;
  notifyFromFrame: (message: ServerMessage) => void;
  setEnabled: (next: boolean) => Promise<void>;
  sendTest: () => Promise<boolean>;
}

export function useNotifications(): NotificationsController {
  const [enabled, setEnabledState] = React.useState<boolean>(() => readNotificationsEnabled());
  const [env, setEnv] = React.useState<NotificationEnvironment>(() =>
    detectNotificationEnvironment(),
  );
  const unreadRef = React.useRef(0);
  const enabledRef = React.useRef(enabled);
  const pushEndpointRef = React.useRef<string | null>(null);
  enabledRef.current = enabled;

  React.useEffect(() => {
    void registerNotificationServiceWorker();
    const initialEnv = detectNotificationEnvironment();
    setEnv(initialEnv);
    if (enabledRef.current && initialEnv.supported && initialEnv.permission === "granted") {
      void subscribeToWebPush().then((endpoint) => {
        pushEndpointRef.current = endpoint;
      });
    }

    const clearBadge = () => {
      if (document.visibilityState === "visible" || document.hasFocus()) {
        unreadRef.current = 0;
        setDocumentUnreadBadge(0);
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
  }, []);

  const notifyFromFrame = React.useCallback((message: ServerMessage) => {
    if (!enabledRef.current) return;
    const payload = formatNotificationForFrame(message);
    if (!payload) return;
    if (!shouldNotifyInBackground()) return;

    unreadRef.current += 1;
    setDocumentUnreadBadge(unreadRef.current);
    // When Web Push is active, sw.js delivers the OS notification via the push
    // channel (which also wakes the worker when the phone is backgrounded).
    if (!pushEndpointRef.current) {
      void showBrowserNotification(payload);
    }
  }, []);

  const setEnabled = React.useCallback(async (next: boolean) => {
    if (!next) {
      writeNotificationsEnabled(false);
      setEnabledState(false);
      pushEndpointRef.current = null;
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
    sendTest,
  };
}
