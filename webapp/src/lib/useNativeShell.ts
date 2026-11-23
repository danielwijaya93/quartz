import * as React from "react";
import { App as CapApp } from "@capacitor/app";
import { StatusBar } from "@capacitor/status-bar";
import { isNativeCapacitor } from "./serverUrl";
import { NativePlugin } from "./nativeBridge";
import { setKeepAlive } from "./useAndroidKeepAlive";

/**
 * The Android-only behaviours that make the packaged app worth installing.
 *
 * Kept in one hook so `App` stays free of platform branches: it is passed a handful
 * of callbacks and, when the app is not native, does nothing at all.
 */
export interface NativeShellOptions {
  /** A turn is running; hold the process so the socket is not frozen. */
  turnRunning: boolean;
  /** Hardware/keyboard Back. Return true to mean "handled, do not exit". */
  onBack: () => boolean;
  /** Text shared into the app from another app, already trimmed. */
  onSharedText: (text: string) => void;
}

export function useNativeShell({ turnRunning, onBack, onSharedText }: NativeShellOptions): void {
  const backRef = React.useRef(onBack);
  backRef.current = onBack;
  const shareRef = React.useRef(onSharedText);
  shareRef.current = onSharedText;

  React.useEffect(() => {
    if (!isNativeCapacitor()) return;
    setKeepAlive(turnRunning);
  }, [turnRunning]);

  React.useEffect(() => {
    if (!isNativeCapacitor()) return;

    // Draw under the status bar instead of letterboxing below it. The web layer
    // already pads with `env(safe-area-inset-*)`, so the content stays clear of the
    // notch while the app no longer wastes a strip of screen.
    void StatusBar.setOverlaysWebView({ overlay: true }).catch(() => {
      /* Plugin or platform without overlay support. */
    });

    const backListener = CapApp.addListener("backButton", () => {
      // Every dialog, drawer and view in the SPA closes on its own Escape path, so
      // asking the app first is how Back becomes "close the thing I am looking at".
      if (!backRef.current()) {
        void CapApp.minimizeApp().catch(() => {
          void CapApp.exitApp();
        });
      }
    });

    const pullShare = (): void => {
      void NativePlugin.consumeSharedText()
        .then((result) => {
          const text = (result?.text ?? "").trim();
          if (text) shareRef.current(text);
        })
        .catch(() => {
          /* Nothing shared. */
        });
    };

    const shareListener = CapApp.addListener("appStateChange", ({ isActive }) => {
      if (isActive) pullShare();
    });

    const sharedEvent = (): void => pullShare();
    window.addEventListener("quartz:shared-text", sharedEvent);
    pullShare();

    return () => {
      window.removeEventListener("quartz:shared-text", sharedEvent);
      void backListener.then((handle) => handle.remove()).catch(() => undefined);
      void shareListener.then((handle) => handle.remove()).catch(() => undefined);
    };
  }, []);
}
