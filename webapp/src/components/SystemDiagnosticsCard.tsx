import * as React from "react";
import { Button } from "@heroui/react";
import { Activity, RefreshCw, ShieldAlert } from "lucide-react";
import { authFetch } from "../lib/authToken";
import { apiUrl } from "../lib/serverUrl";

interface MetricsPayload {
  db?: {
    healthy?: boolean;
    schemaVersion?: number;
  };
  sessions?: {
    active?: number;
  };
  usage24h?: {
    events?: number;
    inputTokens?: number;
    outputTokens?: number;
    costUsd?: number;
    botCostUsd?: number;
    chatCostUsd?: number;
  };
}

interface AuditEvent {
  id: number;
  ts: number;
  eventType: string;
  actor: string;
  decision: string;
  summary: string;
  botId?: string | null;
}

function formatTs(ts: number): string {
  try {
    return new Date(ts * 1000).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return "";
  }
}

export function SystemDiagnosticsCard(): React.ReactElement | null {
  const [metrics, setMetrics] = React.useState<MetricsPayload | null>(null);
  const [events, setEvents] = React.useState<AuditEvent[]>([]);
  const [loading, setLoading] = React.useState(false);
  const [available, setAvailable] = React.useState(true);

  const loadDiagnostics = React.useCallback(async () => {
    setLoading(true);
    try {
      const [mResp, aResp] = await Promise.all([
        authFetch(apiUrl("/api/metrics")).catch(() => null),
        authFetch(apiUrl("/api/audit?limit=8")).catch(() => null),
      ]);
      if (!mResp?.ok && !aResp?.ok) {
        setAvailable(false);
        return;
      }
      setAvailable(true);
      if (mResp?.ok) {
        const parsed = (await mResp.json()) as MetricsPayload;
        if (parsed && typeof parsed === "object" && (parsed.db || parsed.usage24h)) {
          setMetrics(parsed);
        }
      }
      if (aResp?.ok) {
        const parsed = (await aResp.json()) as { events?: unknown };
        if (Array.isArray(parsed.events)) {
          setEvents(parsed.events as AuditEvent[]);
        }
      }
    } catch {
      setAvailable(false);
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    void loadDiagnostics();
  }, [loadDiagnostics]);

  if (!available || (!metrics && events.length === 0)) {
    return null;
  }

  const dbHealthy = metrics?.db?.healthy;
  const schemaVersion = metrics?.db?.schemaVersion;
  const activeSessions = metrics?.sessions?.active ?? 0;
  const costUsd = metrics?.usage24h?.costUsd ?? 0;
  const totalTokens =
    (metrics?.usage24h?.inputTokens ?? 0) + (metrics?.usage24h?.outputTokens ?? 0);

  return (
    <section
      aria-label="System Health and Security Audit"
      className="rounded-xl border border-border bg-surface/50 px-4 py-3"
    >
      <div className="mb-3 flex items-center justify-between gap-2 border-b border-separator pb-2">
        <div className="flex items-center gap-1.5">
          <Activity aria-hidden="true" className="size-3.5 text-muted" />
          <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
            System Health &amp; Security Audit
          </h2>
        </div>
        <Button
          size="sm"
          variant="outline"
          isDisabled={loading}
          onPress={() => void loadDiagnostics()}
        >
          <RefreshCw aria-hidden="true" className={`size-3 ${loading ? "animate-spin" : ""}`} />
          Refresh
        </Button>
      </div>

      {metrics ? (
        <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-4">
          <div className="rounded-lg border border-separator bg-background/60 px-3 py-2">
            <p className="text-[11px] text-muted">SQLite WAL</p>
            <p className="mt-0.5 text-sm font-semibold text-foreground">
              {dbHealthy === false ? "Degraded" : "Healthy"}
              {schemaVersion ? ` (v${schemaVersion})` : ""}
            </p>
          </div>
          <div className="rounded-lg border border-separator bg-background/60 px-3 py-2">
            <p className="text-[11px] text-muted">Active Sessions</p>
            <p className="mt-0.5 font-mono text-sm font-semibold text-foreground">
              {activeSessions}
            </p>
          </div>
          <div className="rounded-lg border border-separator bg-background/60 px-3 py-2">
            <p className="text-[11px] text-muted">24h Bot/LLM Spend</p>
            <p className="mt-0.5 font-mono text-sm font-semibold text-foreground">
              ${costUsd.toFixed(4)}
            </p>
            {(metrics.usage24h?.botCostUsd !== undefined || metrics.usage24h?.chatCostUsd !== undefined) ? (
              <p className="mt-0.5 font-mono text-[10px] text-muted">
                Bots ${(metrics.usage24h?.botCostUsd ?? 0).toFixed(2)} · Chat ${(metrics.usage24h?.chatCostUsd ?? 0).toFixed(2)}
              </p>
            ) : null}
          </div>
          <div className="rounded-lg border border-separator bg-background/60 px-3 py-2">
            <p className="text-[11px] text-muted">24h Tokens</p>
            <p className="mt-0.5 font-mono text-sm font-semibold text-foreground">
              {totalTokens.toLocaleString()}
            </p>
          </div>
        </div>
      ) : null}

      <div className="mt-3 border-t border-separator pt-3">
        <div className="mb-2 flex items-center gap-1.5">
          <ShieldAlert aria-hidden="true" className="size-3.5 text-muted" />
          <p className="text-xs font-medium text-foreground">Recent Security &amp; Governance Events</p>
        </div>
        {events.length === 0 ? (
          <p className="text-xs text-muted">No security or circuit-breaker events recorded yet.</p>
        ) : (
          <ul className="divide-y divide-separator rounded-lg border border-separator bg-background/50">
            {events.map((ev) => (
              <li
                key={ev.id}
                className="flex flex-col gap-0.5 px-3 py-2 text-xs sm:flex-row sm:items-center sm:justify-between"
              >
                <div className="min-w-0">
                  <span className="font-mono font-medium text-foreground">{ev.eventType}</span>
                  {ev.decision ? (
                    <span className="ml-1.5 rounded bg-surface-secondary px-1.5 py-0.5 text-[10px] uppercase text-muted">
                      {ev.decision}
                    </span>
                  ) : null}
                  {ev.summary ? (
                    <p className="truncate text-muted">{ev.summary}</p>
                  ) : null}
                </div>
                <span className="shrink-0 font-mono text-[11px] text-muted">
                  {formatTs(ev.ts)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
