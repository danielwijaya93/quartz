import * as React from "react";
import type { ServerMessage } from "../protocol";
import {
  detectNotificationEnvironment,
  formatNotificationForFrame,
  readNotificationsEnabled,
  registerNotificationServiceWorker,
  requestNotificationPermission,
  setDocumentUnreadBadge,
  shouldNotifyInBackground,
  showBrowserNotification,
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
  enabledRef.current = enabled;

  React.useEffect(() => {
    void registerNotificationServiceWorker();
    setEnv(detectNotificationEnvironment());

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
    void showBrowserNotification(payload);
  }, []);

  const setEnabled = React.useCallback(async (next: boolean) => {
    if (!next) {
      writeNotificationsEnabled(false);
      setEnabledState(false);
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
