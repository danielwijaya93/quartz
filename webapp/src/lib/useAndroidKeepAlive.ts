/**
 * Keeps the Quartz process alive while a turn is running, on Android only.
 *
 * The failure this exists for: Android freezes a backgrounded app, the WebSocket
 * goes quiet, and a long turn looks like it died until you reopen the app. Chrome has
 * the same problem and cannot fix it -- a discarded tab is gone. A foreground service
 * with a visible notification is Android's sanctioned way to say "this process is
 * doing work, do not freeze it", so it is started when a turn begins and stopped when
 * it ends.
 *
 * The service itself is native (`QuartzKeepAliveService`); this module only decides
 * when it should run.
 */

import { NativePlugin } from "./nativeBridge";
import { isNativeCapacitor } from "./serverUrl";

/** Start on the first busy signal, stop when the session reports it is idle again. */
export function setKeepAlive(active: boolean): void {
  if (!isNativeCapacitor()) return;
  void NativePlugin.setKeepAlive({ active }).catch(() => {
    /* The plugin is absent or the OS refused a background start. */
  });
}
