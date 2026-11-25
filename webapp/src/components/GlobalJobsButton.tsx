import * as React from "react";
import { cn } from "@heroui/react";
import { Columns2, RefreshCw, Terminal, X } from "lucide-react";
import { fetchAllJobs, type GlobalJobItem } from "@/lib/jobsApi";

function formatElapsed(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  return mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
}

export function GlobalJobsButton({
  bgCountHint,
  onOpenWorkerTile,
}: {
  bgCountHint: number;
  onOpenWorkerTile: (job: {
    sessionId: string;
    pid: string;
    label: string;
    command: string;
  }) => void;
}): React.ReactElement | null {
  const [open, setOpen] = React.useState(false);
  const [jobs, setJobs] = React.useState<GlobalJobItem[]>([]);
  const [loading, setLoading] = React.useState(false);

  const load = React.useCallback(async () => {
    setLoading(true);
    try {
      const list = await fetchAllJobs();
      setJobs(list);
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    if (open) {
      void load();
    }
  }, [open, load]);

  React.useEffect(() => {
    if (bgCountHint <= 0 && !open) return;
    void load();
  }, [bgCountHint, open, load]);

  const count = Math.max(jobs.length, bgCountHint);
  if (count === 0 && !open) return null;

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={open}
        aria-label={`${count} running background ${count === 1 ? "worker" : "workers"}`}
        title="View running workers across all sessions"
        className={cn(
          "inline-flex h-7 cursor-pointer items-center gap-1.5 rounded-md border px-2 text-[11px] font-medium transition-colors focus-visible:ring-1 focus-visible:ring-focus focus-visible:outline-none",
          "border-border bg-surface-secondary/50 text-foreground hover:bg-surface-secondary",
        )}
      >
        <span aria-hidden="true" className="size-1.5 rounded-full bg-success motion-safe:animate-pulse" />
        <Terminal aria-hidden="true" className="size-3" />
        <span className="tabular-nums">{count}</span>
        <span className="hidden lg:inline">{count === 1 ? "Worker" : "Workers"}</span>
      </button>

      {open ? (
        <div
          role="region"
          aria-label="All running workers"
          className="absolute top-9 right-0 z-40 w-80 overflow-hidden rounded-xl border border-border bg-surface shadow-xl"
        >
          <div className="flex items-center justify-between border-b border-separator px-3 py-2">
            <span className="text-xs font-semibold text-foreground">Running Workers &amp; Jobs</span>
            <div className="flex items-center gap-1">
              <button
                type="button"
                onClick={() => void load()}
                disabled={loading}
                aria-label="Refresh running workers"
                className="inline-flex size-6 cursor-pointer items-center justify-center rounded text-muted hover:bg-surface-secondary hover:text-foreground"
              >
                <RefreshCw aria-hidden="true" className="size-3" />
              </button>
              <button
                type="button"
                onClick={() => setOpen(false)}
                aria-label="Close workers menu"
                className="inline-flex size-6 cursor-pointer items-center justify-center rounded text-muted hover:bg-surface-secondary hover:text-foreground"
              >
                <X aria-hidden="true" className="size-3" />
              </button>
            </div>
          </div>

          <div className="max-h-72 overflow-y-auto p-2">
            {loading && jobs.length === 0 ? (
              <p className="px-2 py-3 text-xs text-muted">Loading workers…</p>
            ) : jobs.length === 0 ? (
              <p className="px-2 py-3 text-xs text-muted">No running workers found.</p>
            ) : (
              <ul className="space-y-1.5">
                {jobs.map((job) => {
                  const pid = String(job.pid);
                  return (
                    <li
                      key={`${job.sessionId ?? "default"}-${pid}`}
                      className="rounded-lg border border-border bg-background/60 p-2"
                    >
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-xs font-medium text-foreground">
                            {job.label} · pid {pid} · {formatElapsed(job.elapsed_s)}
                          </p>
                          {job.sessionTitle ? (
                            <p className="truncate text-[10.5px] text-meta">{job.sessionTitle}</p>
                          ) : null}
                          <p className="mt-0.5 truncate font-mono text-[10.5px] text-muted">
                            {job.command}
                          </p>
                        </div>
                        {job.sessionId ? (
                          <button
                            type="button"
                            onClick={() => {
                              onOpenWorkerTile({
                                sessionId: job.sessionId!,
                                pid,
                                label: job.label,
                                command: job.command,
                              });
                              setOpen(false);
                            }}
                            className="inline-flex h-6 shrink-0 cursor-pointer items-center gap-1 rounded-md border border-border bg-surface-secondary px-2 text-[11px] font-medium text-foreground hover:bg-surface"
                          >
                            <Columns2 aria-hidden="true" className="size-3" />
                            Tile
                          </button>
                        ) : null}
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}
