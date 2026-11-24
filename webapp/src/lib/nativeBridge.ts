/**
 * Thin wrapper over the app's own native plugin (`QuartzNativePlugin`).
 *
 * These are the handful of things a WebView genuinely cannot do on its own: run a
 * foreground service, show a biometric prompt, and read the intent the app was
 * launched with. Capacitor's `registerPlugin` resolves to a no-op proxy in a browser,
 * so every call site can stay one line and not branch on platform.
 */

import { registerPlugin } from "@capacitor/core";

export interface QuartzNativePlugin {
  /** Run (or stop) the foreground service that survives screen-off. */
  setKeepAlive(options: {
    active: boolean;
    serverUrl?: string;
    token?: string;
    notificationsEnabled?: boolean;
  }): Promise<{ active: boolean }>;
  /** Whether Android battery optimization is disabled for Quartz. */
  batteryOptimizationStatus(): Promise<{ ignored: boolean }>;
  /** Open the Android system prompt to exempt Quartz from battery optimization. */
  requestIgnoreBatteryOptimizations(): Promise<void>;
  /** Whether this device can do biometrics, and whether one is enrolled. */
  biometricStatus(): Promise<{ available: boolean; enrolled: boolean; kind: string }>;
  /** Show the system biometric prompt. Resolves `{ ok }` — never throws on cancel. */
  authenticate(options: { reason: string }): Promise<{ ok: boolean }>;
  /** Text or a URI handed to the app by a share from another app, then cleared. */
  consumeSharedText(): Promise<{ text: string; kind: string }>;
}

export const NativePlugin = registerPlugin<QuartzNativePlugin>("QuartzNative");
