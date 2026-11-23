import * as React from "react";
import { getServerUrl, isNativeCapacitor, setServerUrl } from "../lib/serverUrl";

/**
 * Native-only card that changes which `quartz web` server the app talks to.
 * Saving dispatches `quartz:change-server`, which sends the app back to the
 * connect screen instead of reloading a socket against a stale base URL.
 */
export function ServerUrlCard(): React.ReactElement | null {
  const [current, setCurrent] = React.useState(() => getServerUrl() ?? "");
  const [draft, setDraft] = React.useState(() => getServerUrl() ?? "");
  const [status, setStatus] = React.useState<{ ok: boolean; text: string } | null>(null);

  if (!isNativeCapacitor()) return null;

  function save(event: React.FormEvent): void {
    event.preventDefault();
    setStatus(null);
    try {
      const normalized = setServerUrl(draft);
      setCurrent(normalized);
      setDraft(normalized);
      setStatus({ ok: true, text: "Saved. Reconnecting…" });
      window.setTimeout(() => {
        window.dispatchEvent(new Event("quartz:change-server"));
      }, 400);
    } catch (err) {
      setStatus({ ok: false, text: err instanceof Error ? err.message : "Invalid server URL" });
    }
  }

  return (
    <form
      onSubmit={save}
      className="rounded-xl border border-border bg-surface/50 px-4 py-3 space-y-2"
    >
      <div className="min-w-0">
        <p className="text-sm font-medium text-foreground">Quartz server</p>
        <p className="text-xs text-muted">
          Where this app connects. Currently {current || "not set"}.
        </p>
      </div>
      <div className="flex flex-col gap-2 sm:flex-row">
        <input
          type="url"
          inputMode="url"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          placeholder="http://100.x.y.z:8765"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          className="min-w-0 flex-1 rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none focus:border-accent"
        />
        <button
          type="submit"
          className="shrink-0 rounded-lg bg-accent px-3 py-2 text-sm font-medium text-white"
        >
          Save &amp; reconnect
        </button>
      </div>
      {status && (
        <p className={`text-xs ${status.ok ? "text-muted" : "text-red-400"}`} role="status">
          {status.text}
        </p>
      )}
    </form>
  );
}
