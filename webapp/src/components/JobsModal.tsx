import * as React from "react";
import { Button, Modal, useOverlayState } from "@heroui/react";
import { Eye, RefreshCw, Square } from "lucide-react";

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

export function JobsModal({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}): React.ReactElement {
  const state = useOverlayState({ isOpen: open, onOpenChange });
  const [jobs, setJobs] = React.useState<JobItem[]>([]);
  const [loading, setLoading] = React.useState(false);
  const [tails, setTails] = React.useState<Record<string, string>>({});
  const [busyPid, setBusyPid] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    setLoading(true);
    try {
      const resp = await fetch("/api/jobs");
      if (!resp.ok) throw new Error("Failed to load jobs");
      const data = (await resp.json()) as { jobs?: JobItem[] };
      setJobs(Array.isArray(data.jobs) ? data.jobs : []);
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
      const resp = await fetch(`/api/jobs/${encodeURIComponent(pid)}/tail`);
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
      await fetch(`/api/jobs/${encodeURIComponent(pid)}/kill`, { method: "POST" });
      await load();
    } finally {
      setBusyPid(null);
    }
  };

  return (
    <Modal state={state}>
      <Modal.Backdrop variant="blur">
        <Modal.Container size="lg" scroll="inside">
          <Modal.Dialog className="p-0">
            <Modal.Header className="border-b border-separator px-4 py-3">
              <Modal.Heading className="text-base font-semibold">Background Jobs</Modal.Heading>
            </Modal.Header>

            <Modal.Body className="p-4">
              {loading && jobs.length === 0 ? (
                <p className="text-sm text-muted">Loading jobs…</p>
              ) : jobs.length === 0 ? (
                <p className="text-sm text-muted">No background jobs running.</p>
              ) : (
                <ul className="divide-y divide-separator rounded-lg border border-border">
                  {jobs.map((job) => {
                    const pid = String(job.pid);
                    const tail = tails[pid];
                    const showingTail = tail !== undefined;
                    return (
                      <li key={pid} className="space-y-2 px-3 py-2.5">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <div className="min-w-0 flex-1">
                            <p className="text-sm font-medium text-foreground">
                              pid {pid} · {job.label} · {formatElapsed(job.elapsed_s)}
                            </p>
                            <p className="mt-0.5 truncate font-mono text-xs text-muted">
                              {job.command}
                            </p>
                          </div>
                          <div className="flex shrink-0 items-center gap-2">
                            <Button
                              size="sm"
                              variant="outline"
                              isDisabled={busyPid === pid}
                              onPress={() => void toggleTail(pid)}
                            >
                              <Eye aria-hidden="true" className="size-3.5" />
                              {showingTail ? "Hide Tail" : "View Tail"}
                            </Button>
                            <Button
                              size="sm"
                              variant="danger-soft"
                              isDisabled={busyPid === pid}
                              onPress={() => void killJob(pid)}
                            >
                              <Square aria-hidden="true" className="size-3" />
                              Kill
                            </Button>
                          </div>
                        </div>
                        {showingTail ? (
                          <pre className="max-h-48 overflow-auto rounded-md border border-border bg-background p-2.5 font-mono text-xs whitespace-pre-wrap text-foreground">
                            {tail}
                          </pre>
                        ) : null}
                      </li>
                    );
                  })}
                </ul>
              )}
            </Modal.Body>

            <Modal.Footer className="flex items-center justify-between border-t border-separator px-4 py-2.5">
              <Button size="sm" variant="outline" isDisabled={loading} onPress={() => void load()}>
                <RefreshCw aria-hidden="true" className="size-3.5" />
                Refresh
              </Button>
              <Button size="sm" variant="primary" onPress={() => onOpenChange(false)}>
                Done
              </Button>
            </Modal.Footer>
          </Modal.Dialog>
        </Modal.Container>
      </Modal.Backdrop>
    </Modal>
  );
}
