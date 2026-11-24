import * as React from "react";
import { authFetch } from "../lib/authToken";
import { apiUrl } from "../lib/serverUrl";
import { Button } from "@heroui/react";
import { Eye, RefreshCw, Square, X } from "lucide-react";

interface JobItem {
  pid: number | string;
  command: string;
  label: string;
  elapsed_s: number;
}

function formatElapsed(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  return mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
}

/**
 * Non-blocking floating tray for background jobs, anchored above the composer.
 *
 * Unlike a modal, it has no backdrop and traps no focus: you can watch live job
 * tails, kill a stuck worker, and keep reading or steering the chat at once.
 */
export function JobsTray({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}): React.ReactElement | null {
  const [jobs, setJobs] = React.useState<JobItem[]>([]);
  const [fetchedAt, setFetchedAt] = React.useState<number>(() => Date.now());
  const [now, setNow] = React.useState<number>(() => Date.now());
  const [loading, setLoading] = React.useState(false);
  const [tails, setTails] = React.useState<Record<string, string>>({});
  const [busyPid, setBusyPid] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    setLoading(true);
    try {
      const resp = await authFetch(apiUrl("/api/jobs"));
      if (!resp.ok) throw new Error("Failed to load jobs");
      const data = (await resp.json()) as { jobs?: JobItem[] };
      const ts = Date.now();
      setJobs(Array.isArray(data.jobs) ? data.jobs : []);
      setFetchedAt(ts);
      setNow(ts);
    } catch {
      setJobs([]);
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    if (open) {
      setTails({});
      void load();
    }
  }, [open, load]);

  React.useEffect(() => {
    if (!open || jobs.length === 0) return;
    setNow(Date.now());
    const id = window.setInterval(() => {
      setNow(Date.now());
    }, 1000);
    return () => window.clearInterval(id);
  }, [open, jobs.length]);

  if (!open) return null;

  const toggleTail = async (pid: string): Promise<void> => {
    if (tails[pid] !== undefined) {
      setTails((prev) => {
        const next = { ...prev };
        delete next[pid];
        return next;
      });
      return;
    }
    setBusyPid(pid);
    try {
      const resp = await authFetch(apiUrl(`/api/jobs/${encodeURIComponent(pid)}/tail`));
      if (!resp.ok) {
        setTails((prev) => ({ ...prev, [pid]: "(output tail unavailable)" }));
      } else {
        const data = (await resp.json()) as { tail?: string };
        const text = (data.tail ?? "").trim();
        setTails((prev) => ({ ...prev, [pid]: text || "(no output yet)" }));
      }
    } catch {
      setTails((prev) => ({ ...prev, [pid]: "(failed to load output tail)" }));
    } finally {
      setBusyPid(null);
    }
  };

  const killJob = async (pid: string): Promise<void> => {
    setBusyPid(pid);
    try {
      await authFetch(apiUrl(`/api/jobs/${encodeURIComponent(pid)}/kill`), { method: "POST" });
      await load();
    } finally {
      setBusyPid(null);
    }
  };

  return (
    <div className="shrink-0 px-4 pb-1">
      <section
        role="region"
        aria-label="Background Jobs"
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.stopPropagation();
            onClose();
          }
        }}
        className="mx-auto w-full max-w-3xl overflow-hidden rounded-xl border border-border bg-surface shadow-lg"
      >
        <div className="flex items-center justify-between border-b border-separator px-3 py-2">
          <h2 className="text-xs font-semibold tracking-wide text-foreground">
            Background Jobs
          </h2>
          <div className="flex items-center gap-1">
            <Button
              isIconOnly
              size="sm"
              variant="ghost"
              aria-label="Refresh background jobs"
              isDisabled={loading}
              onPress={() => void load()}
              className="size-6 min-w-6 text-muted hover:text-foreground"
            >
              <RefreshCw aria-hidden="true" className="size-3.5" />
            </Button>
            <Button
              isIconOnly
              size="sm"
              variant="ghost"
              aria-label="Close background jobs"
              onPress={onClose}
              className="size-6 min-w-6 text-muted hover:text-foreground"
            >
              <X aria-hidden="true" className="size-3.5" />
            </Button>
          </div>
        </div>

        <div className="max-h-72 overflow-y-auto p-2.5">
          {loading && jobs.length === 0 ? (
            <p className="px-1 py-2 text-xs text-muted">Loading jobs…</p>
          ) : jobs.length === 0 ? (
            <p className="px-1 py-2 text-xs text-muted">No background jobs running.</p>
          ) : (
            <ul className="divide-y divide-separator rounded-lg border border-border">
              {jobs.map((job) => {
                const pid = String(job.pid);
                const tail = tails[pid];
                const showingTail = tail !== undefined;
                return (
                  <li key={pid} className="space-y-2 px-3 py-2">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="min-w-0 flex-1">
                        <p className="text-xs font-medium tabular-nums text-foreground">
                          pid {pid} · {job.label} · {formatElapsed(job.elapsed_s + Math.max(0, (now - fetchedAt) / 1000))}
                        </p>
                        <p className="mt-0.5 truncate font-mono text-[11px] text-muted">
                          {job.command}
                        </p>
                      </div>
                      <div className="flex shrink-0 items-center gap-1.5">
                        <Button
                          size="sm"
                          variant="outline"
                          isDisabled={busyPid === pid}
                          onPress={() => void toggleTail(pid)}
                          className="h-7 text-xs"
                        >
                          <Eye aria-hidden="true" className="size-3" />
                          {showingTail ? "Hide Tail" : "View Tail"}
                        </Button>
                        <Button
                          size="sm"
                          variant="danger-soft"
                          isDisabled={busyPid === pid}
                          onPress={() => void killJob(pid)}
                          className="h-7 text-xs"
                        >
                          <Square aria-hidden="true" className="size-3" />
                          Kill
                        </Button>
                      </div>
                    </div>
                    {showingTail ? (
                      <pre className="max-h-44 overflow-auto rounded-md border border-border bg-background p-2 font-mono text-xs whitespace-pre-wrap text-foreground">
                        {tail}
                      </pre>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </section>
    </div>
  );
}