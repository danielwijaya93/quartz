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
import type { BrowserNotificationPayload } from "./notifications";
import { isNativeCapacitor } from "./serverUrl";

/** Stable small integer per tag: Android needs an int id, not a string. */
function idForTag(tag: string): number {
  let hash = 0;
  for (let i = 0; i < tag.length; i += 1) {
    hash = (hash * 31 + tag.charCodeAt(i)) | 0;
  }
  return Math.abs(hash) % 2_000_000_000 + 1;
}

export type NativePermission = "granted" | "denied" | "prompt" | "prompt-with-rationale" | "unsupported";

export async function requestNativePermission(): Promise<NativePermission> {
  if (!isNativeCapacitor()) return "unsupported";
  try {
    const current = await LocalNotifications.checkPermissions();
    if (current.display === "granted") return "granted";
    const asked = await LocalNotifications.requestPermissions();
    if (asked.display === "granted") return "granted";
    return asked.display === "denied" ? "denied" : "prompt";
  } catch {
    return "unsupported";
  }
}

export async function showNativeNotification(
  payload: BrowserNotificationPayload,
): Promise<boolean> {
  if (!isNativeCapacitor()) return false;
  try {
    await LocalNotifications.schedule({
      notifications: [
        {
          id: idForTag(payload.tag),
          title: payload.title,
          body: payload.body,
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
