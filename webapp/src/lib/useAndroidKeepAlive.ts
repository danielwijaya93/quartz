/**
 * Keeps the Quartz notification service alive on Android.
 *
 * The foreground service holds a native Java WebSocket to `/ws/notifications`,
 * a PARTIAL_WAKE_LOCK, and a WifiLock so lock-screen alerts fire even when
 * Android pauses the WebView.
 */

import { getWebToken } from "./authToken";
import { NativePlugin } from "./nativeBridge";
import { getServerUrl, isNativeCapacitor } from "./serverUrl";

export function setKeepAlive(active: boolean, notificationsEnabled = active): void {
  if (!isNativeCapacitor()) return;
  const serverUrl = getServerUrl() ?? "";
  const token = getWebToken() ?? "";
  void NativePlugin.setKeepAlive({
    active,
    serverUrl,
    token,
    notificationsEnabled,
  }).catch(() => {
    /* The plugin is absent or the OS refused a background start. */
  });
}
