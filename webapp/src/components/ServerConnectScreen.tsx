import * as React from "react";
import { getServerUrl, normalizeServerUrl, setServerUrl } from "../lib/serverUrl";

interface ServerConnectScreenProps {
  onConnected: () => void;
}

export function ServerConnectScreen({ onConnected }: ServerConnectScreenProps): React.ReactElement {
  const [input, setInput] = React.useState(() => getServerUrl() ?? "");
  const [testing, setTesting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  async function handleSubmit(event: React.FormEvent): Promise<void> {
    event.preventDefault();
    setError(null);

    let normalized: string;
    try {
      normalized = normalizeServerUrl(input);
      if (!normalized) {
        setError("Enter your Quartz Web server URL (for example http://100.64.0.1:8765).");
        return;
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Invalid server URL");
      return;
    }

    setTesting(true);
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 6000);
    try {
      const resp = await fetch(`${normalized}/api/health`, {
        signal: controller.signal,
      });
      if (!resp.ok) {
        setError(`Server returned ${resp.status} ${resp.statusText}`.trim());
        return;
      }
      setServerUrl(normalized);
      onConnected();
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") {
        setError("Connection timed out. Check that quartz web is running and reachable on Tailscale/LAN.");
      } else {
        setError(
          err instanceof Error && err.message
            ? `Could not reach server: ${err.message}`
            : "Could not reach server.",
        );
      }
    } finally {
      window.clearTimeout(timer);
      setTesting(false);
    }
  }

  return (
    <div className="flex min-h-dvh items-center justify-center bg-[var(--bg)] px-4 py-8 text-[var(--fg)]">
      <div className="w-full max-w-md rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-6 shadow-lg">
        <h1 className="text-xl font-semibold tracking-tight">Connect to Quartz</h1>
        <p className="mt-1.5 text-sm text-[var(--muted)]">
          Enter the URL where <code className="rounded bg-black/10 px-1 py-0.5">quartz web</code> is
          running (e.g. your Tailscale IP or hostname).
        </p>

        <form onSubmit={(e) => void handleSubmit(e)} className="mt-5 space-y-4">
          <div>
            <label htmlFor="quartz-server-url" className="block text-sm font-medium">
              Server URL
            </label>
            <input
              id="quartz-server-url"
              type="url"
              inputMode="url"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
              placeholder="http://100.x.y.z:8765"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              disabled={testing}
              className="mt-1.5 w-full rounded-lg border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
            />
          </div>

          {error && (
            <div
              role="alert"
              className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-400"
            >
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={testing}
            className="w-full rounded-lg bg-[var(--accent)] px-4 py-2.5 text-sm font-medium text-white transition-opacity disabled:opacity-60"
          >
            {testing ? "Connecting..." : "Connect"}
          </button>
        </form>
      </div>
    </div>
  );
}
