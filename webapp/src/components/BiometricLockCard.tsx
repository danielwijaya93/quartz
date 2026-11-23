import * as React from "react";
import {
  biometricAvailability,
  promptBiometric,
  readBiometricLockEnabled,
  writeBiometricLockEnabled,
} from "./BiometricGate";
import { isNativeCapacitor } from "../lib/serverUrl";

/**
 * The switch for the biometric gate, shown only where it can work.
 *
 * Turning it on asks for a successful unlock first: enabling a lock you have never
 * been able to satisfy is how a phone ends up with an app nobody can open.
 */
export function BiometricLockCard(): React.ReactElement | null {
  const [enabled, setEnabled] = React.useState(() => readBiometricLockEnabled());
  const [available, setAvailable] = React.useState(false);
  const [status, setStatus] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!isNativeCapacitor()) return;
    void biometricAvailability().then((result) => {
      setAvailable(result.available && result.enrolled);
    });
  }, []);

  if (!isNativeCapacitor()) return null;

  async function toggle(): Promise<void> {
    setStatus(null);
    if (enabled) {
      writeBiometricLockEnabled(false);
      setEnabled(false);
      setStatus("Lock turned off.");
      return;
    }
    const ok = await promptBiometric();
    if (!ok) {
      setStatus("Could not verify. The lock was not turned on.");
      return;
    }
    writeBiometricLockEnabled(true);
    setEnabled(true);
    setStatus("Lock turned on. The app will ask whenever you reopen it.");
  }

  return (
    <div className="space-y-2 rounded-xl border border-border bg-surface/50 px-4 py-3">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-medium text-foreground">Biometric lock</p>
          <p className="text-xs text-muted">
            {available
              ? "Require your fingerprint or face to open the app."
              : "No enrolled fingerprint or face on this device."}
          </p>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={enabled}
          aria-label="Biometric lock"
          disabled={!available && !enabled}
          onClick={() => void toggle()}
          className={`shrink-0 rounded-full px-3 py-1.5 text-xs font-medium ${
            enabled ? "bg-accent text-white" : "border border-border text-foreground"
          } disabled:opacity-50`}
        >
          {enabled ? "On" : "Off"}
        </button>
      </div>
      {status && (
        <p className="text-xs text-muted" role="status">
          {status}
        </p>
      )}
    </div>
  );
}
