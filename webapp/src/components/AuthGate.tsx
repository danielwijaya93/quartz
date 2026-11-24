import * as React from "react";
import { KeyRound } from "lucide-react";
import { setWebToken } from "@/lib/authToken";
import { apiUrl } from "@/lib/serverUrl";

export interface AuthGateProps {
  onAuthenticated: () => void;
}

export function AuthGate({ onAuthenticated }: AuthGateProps): React.ReactElement {
  const [token, setToken] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [submitting, setSubmitting] = React.useState(false);

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    const cleaned = token.trim();
    if (!cleaned) {
      setError("Please enter your web auth token.");
      return;
    }
    setError(null);
    setSubmitting(true);
    try {
      const resp = await fetch(apiUrl("/api/auth/login"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ token: cleaned }),
      });
      if (!resp.ok) {
        let message = "Invalid token";
        try {
          const body = (await resp.json()) as { error?: unknown };
          if (typeof body.error === "string" && body.error.trim()) {
            message = body.error;
          }
        } catch {
          // ignore JSON parse errors
        }
        setError(message);
        return;
      }
      setWebToken(cleaned);
      onAuthenticated();
    } catch {
      setError("Could not reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="flex min-h-dvh items-center justify-center bg-background px-4 py-8 text-foreground">
      <div className="w-full max-w-md rounded-2xl border border-border bg-surface p-6 shadow-sm">
        <div className="mb-4 flex items-center gap-3">
          <div className="flex size-10 items-center justify-center rounded-xl border border-border bg-surface-secondary">
            <KeyRound aria-hidden="true" className="size-5 text-accent" />
          </div>
          <div>
            <h1 className="text-base font-semibold text-foreground">Sign in to Quartz</h1>
            <p className="text-xs text-muted">
              Paste the web auth token printed by <code>quartz web</code>.
            </p>
          </div>
        </div>

        <form onSubmit={(e) => void handleSubmit(e)} className="space-y-4">
          <div className="space-y-1.5">
            <label htmlFor="quartz-web-token" className="block text-xs font-medium text-foreground">
              Web auth token
            </label>
            <input
              id="quartz-web-token"
              type="password"
              autoComplete="current-password"
              autoFocus
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="Paste token"
              disabled={submitting}
              className="w-full rounded-xl border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
            />
          </div>

          {error ? (
            <p role="alert" className="text-xs font-medium text-danger">
              {error}
            </p>
          ) : null}

          <button
            type="submit"
            disabled={submitting}
            className="w-full rounded-xl bg-accent px-4 py-2 text-sm font-medium text-accent-foreground transition hover:opacity-90 disabled:opacity-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
          >
            {submitting ? "Verifying…" : "Unlock"}
          </button>
        </form>
      </div>
    </div>
  );
}
