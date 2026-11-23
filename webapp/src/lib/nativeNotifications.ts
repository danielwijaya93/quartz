/**
 * Notifications for the packaged Android app.
 *
 * Why local notifications rather than Firebase: the app is a live WebSocket client,
 * so the turn result is already in the process when the turn ends. A foreground
 * service (see `lib/useAndroidKeepAlive`) keeps that process running with the screen
 * off, which means a local notification lands at the same moment a push would — with
 * no Firebase project, no google-services.json, and no server round trip.
 *
 * What FCM would add, and this does not: a notification after Android has actually
 * killed the app. That is a real gap, documented in the README rather than pretended
 * away.
 */

import { LocalNotifications } from "@capacitor/local-notifications";
import type { BrowserNotificationPayload, NotificationPermissionState } from "./notifications";
import { isNativeCapacitor } from "./serverUrl";

export const NATIVE_PERM_STORAGE_KEY = "quartz:native-notif-perm";
export const NATIVE_ALERT_CHANNEL_ID = "quartz_alerts";

let channelReady: Promise<void> | null = null;

/** Stable small integer per tag: Android needs an int id, not a string. */
function idForTag(tag: string): number {
  let hash = 0;
  for (let i = 0; i < tag.length; i += 1) {
    hash = (hash * 31 + tag.charCodeAt(i)) | 0;
  }
  return Math.abs(hash) % 2_000_000_000 + 1;
}

function cachePermission(state: NotificationPermissionState): void {
  try {
    if (state === "granted" || state === "denied" || state === "default") {
      window.localStorage.setItem(NATIVE_PERM_STORAGE_KEY, state);
    }
  } catch {
    /* Storage blocked. */
  }
}

function mapDisplayPermission(display: string | undefined): NotificationPermissionState {
  if (display === "granted") return "granted";
  if (display === "denied") return "denied";
  return "default";
}

export type NativePermission =
  | "granted"
  | "denied"
  | "prompt"
  | "prompt-with-rationale"
  | "unsupported";

/** Query Android's actual POST_NOTIFICATIONS state and cache it for synchronous render. */
export async function checkNativePermission(): Promise<NotificationPermissionState> {
  if (!isNativeCapacitor()) return "unsupported";
  try {
    const current = await LocalNotifications.checkPermissions();
    const mapped = mapDisplayPermission(current.display);
    cachePermission(mapped);
    return mapped;
  } catch {
    return "unsupported";
  }
}

export async function requestNativePermission(): Promise<NativePermission> {
  if (!isNativeCapacitor()) return "unsupported";
  try {
    const current = await LocalNotifications.checkPermissions();
    if (current.display === "granted") {
      cachePermission("granted");
      return "granted";
    }
    const asked = await LocalNotifications.requestPermissions();
    const mapped = mapDisplayPermission(asked.display);
    cachePermission(mapped);
    if (asked.display === "granted") return "granted";
    return asked.display === "denied" ? "denied" : "prompt";
  } catch {
    return "unsupported";
  }
}

/** Create a high-importance channel so turn-end and approval alerts pop up with sound. */
export function ensureNativeChannel(): Promise<void> {
  if (!isNativeCapacitor()) return Promise.resolve();
  if (!channelReady) {
    channelReady = LocalNotifications.createChannel({
      id: NATIVE_ALERT_CHANNEL_ID,
      name: "Quartz alerts",
      description: "Notifications when a turn finishes or approval is needed",
      importance: 4,
      visibility: 1,
      vibration: true,
    }).catch(() => {
      channelReady = null;
    });
  }
  return channelReady;
}

export async function showNativeNotification(
  payload: BrowserNotificationPayload,
): Promise<boolean> {
  if (!isNativeCapacitor()) return false;
  try {
    const perm = await checkNativePermission();
    if (perm !== "granted") return false;
    await ensureNativeChannel();
    await LocalNotifications.schedule({
      notifications: [
        {
          id: idForTag(payload.tag),
          title: payload.title,
          body: payload.body,
          channelId: NATIVE_ALERT_CHANNEL_ID,
          // Replacing by id is what makes a burst of frames read as one update
          // instead of a stack of notifications for the same turn.
          group: "quartz",
          autoCancel: true,
        },
      ],
    });
    return true;
  } catch {
    return false;
  }
}

/** Clear anything still on the shade, e.g. when the user returns to the app. */
export async function clearNativeNotifications(): Promise<void> {
  if (!isNativeCapacitor()) return;
  try {
    await LocalNotifications.removeAllDeliveredNotifications();
  } catch {
    /* Nothing delivered, or the plugin is missing. */
  }
}
