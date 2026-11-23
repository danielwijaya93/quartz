import * as React from "react";
import { NativePlugin } from "../lib/nativeBridge";
import { isNativeCapacitor } from "../lib/serverUrl";

/**
 * Optional biometric lock for the packaged app.
 *
 * Quartz Web has no authentication and the agent has a shell, so an unlocked phone
 * handed to someone is a shell prompt. This gate is device-local only: it guards the
 * UI, not the network, and it is off unless the user turns it on.
 */

const STORAGE_KEY = "quartz:biometricLock";

export function readBiometricLockEnabled(): boolean {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "true";
  } catch {
    return false;
  }
}

export function writeBiometricLockEnabled(enabled: boolean): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, enabled ? "true" : "false");
  } catch {
    /* Storage blocked. */
  }
}

export async function biometricAvailability(): Promise<{
  available: boolean;
  enrolled: boolean;
}> {
  if (!isNativeCapacitor()) return { available: false, enrolled: false };
  try {
    const status = await NativePlugin.biometricStatus();
    return { available: Boolean(status?.available), enrolled: Boolean(status?.enrolled) };
  } catch {
    return { available: false, enrolled: false };
  }
}

export async function promptBiometric(): Promise<boolean> {
  try {
    const result = await NativePlugin.authenticate({ reason: "Unlock Quartz" });
    return Boolean(result?.ok);
  } catch {
    return false;
  }
}

/** Wrap the app: a locked native app shows `children` only after a successful prompt. */
export function BiometricGate({ children }: { children: React.ReactNode }): React.ReactElement {
  const [locked, setLocked] = React.useState(() => isNativeCapacitor() && readBiometricLockEnabled());
  const [error, setError] = React.useState<string | null>(null);

  const unlock = React.useCallback(async () => {
    setError(null);
    const ok = await promptBiometric();
    if (ok) {
      setLocked(false);
      return;
    }
    setError("Not unlocked. Tap to try again.");
  }, []);

  React.useEffect(() => {
    if (!locked) return;
    void unlock();
    // Re-lock when the app goes to the background, so a resumed app asks again.
    const onVisible = (): void => {
      if (document.visibilityState === "visible" && readBiometricLockEnabled()) {
        setLocked(true);
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [locked, unlock]);

  if (!locked) return <>{children}</>;

  return (
    <div className="flex min-h-dvh flex-col items-center justify-center gap-4 px-6 text-center">
      <span className="text-4xl">🔒</span>
      <h1 className="text-lg font-semibold">Quartz is locked</h1>
      <p className="max-w-xs text-sm text-[var(--muted)]">
        Unlock with your fingerprint or face to reach this device's Quartz sessions.
      </p>
      {error && <p className="text-xs text-red-400">{error}</p>}
      <button
        type="button"
        onClick={() => void unlock()}
        className="rounded-lg bg-[var(--accent)] px-4 py-2 text-sm font-medium text-white"
      >
        Unlock
      </button>
    </div>
  );
}
