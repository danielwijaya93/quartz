import * as React from "react";
import { Button, cn } from "@heroui/react";
import {
  ArrowUpLeft,
  ExternalLink,
  Maximize2,
  Minimize2,
  RefreshCw,
  Square,
  Terminal,
  X,
} from "lucide-react";
import { authFetch } from "@/lib/authToken";
import { apiUrl } from "@/lib/serverUrl";

const POLL_INTERVAL_MS = 1500;

export interface WorkerTailTileProps {
  tileNumber: number;
  focused: boolean;
  maximized: boolean;
  canClose: boolean;
  sessionId: string;
  pid: string;
  label: string;
  command: string;
  onFocus: () => void;
  onClose: () => void;
  onToggleMaximize: () => void;
  onPromote: () => void;
  onOpenParentSession: (sessionId: string) => void;
}

type WorkerState = "running" | "exited" | "stopped";

export function WorkerTailTile({
  tileNumber,
  focused,
  maximized,
  canClose,
  sessionId,
  pid,
  label,
  command,
  onFocus,
  onClose,
  onToggleMaximize,
  onPromote,
  onOpenParentSession,
}: WorkerTailTileProps): React.ReactElement {
  const [tail, setTail] = React.useState<string>("");
  const [workerState, setWorkerState] = React.useState<WorkerState>("running");
  const [loading, setLoading] = React.useState(true);
  const [killing, setKilling] = React.useState(false);
  const [autoScroll, setAutoScroll] = React.useState(true);
  const preRef = React.useRef<HTMLPreElement | null>(null);

  const fetchTail = React.useCallback(async () => {
    try {
      const path = `/api/jobs/${encodeURIComponent(pid)}/tail?sessionId=${encodeURIComponent(sessionId)}`;
      const resp = await authFetch(apiUrl(path), { method: "GET" });
      if (resp.status === 404) {
        setWorkerState((prev) => (prev === "stopped" ? "stopped" : "exited"));
        return;
      }
      if (!resp.ok) return;
      const data = (await resp.json()) as { tail?: string };
      if (typeof data.tail === "string") {
        setTail(data.tail);
      }
    } catch {
      // Transient network hiccup: keep last known tail and retry on next poll.
    } finally {
      setLoading(false);
    }
  }, [pid, sessionId]);

  React.useEffect(() => {
    setWorkerState("running");
    setLoading(true);
    void fetchTail();
  }, [fetchTail]);

  React.useEffect(() => {
    if (workerState !== "running") return;
    const id = window.setInterval(() => {
      void fetchTail();
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(id);
  }, [workerState, fetchTail]);

  React.useEffect(() => {
    if (!autoScroll || !preRef.current) return;
    preRef.current.scrollTop = preRef.current.scrollHeight;
  }, [tail, autoScroll]);

  const handleKill = React.useCallback(async () => {
    setKilling(true);
    try {
      const path = `/api/jobs/${encodeURIComponent(pid)}/kill?sessionId=${encodeURIComponent(sessionId)}`;
      const resp = await authFetch(apiUrl(path), { method: "POST" });
      if (resp.ok) {
        setWorkerState("stopped");
      }
    } finally {
      setKilling(false);
    }
  }, [pid, sessionId]);

  const statusPill =
    workerState === "running"
      ? { text: "Live", cls: "border-sky-500/30 bg-sky-500/10 text-sky-500", dot: "bg-sky-500 motion-safe:animate-pulse" }
      : workerState === "stopped"
        ? { text: "Stopped", cls: "border-danger/30 bg-danger/10 text-danger", dot: "bg-danger" }
        : { text: "Exited", cls: "border-emerald-500/30 bg-emerald-500/10 text-emerald-500", dot: "bg-emerald-500" };

  return (
    <section
      aria-label={`Worker tile ${tileNumber}: ${label}`}
      onMouseDown={onFocus}
      className={cn(
        "flex h-full min-h-0 flex-col overflow-hidden rounded-xl border bg-background transition-colors",
        focused
          ? "border-sky-500/50 ring-1 ring-sky-500/20"
          : "border-border/80",
      )}
    >
      <header
        className={cn(
          "flex h-9 shrink-0 items-center justify-between gap-2 border-b border-separator px-2.5",
          focused ? "bg-sky-500/5" : "bg-surface/70",
        )}
      >
        <div className="flex min-w-0 items-center gap-1.5">
          <span
            className={cn(
              "inline-flex size-4 shrink-0 items-center justify-center rounded text-[10px] font-bold tabular-nums",
              focused
                ? "bg-sky-500 text-zinc-950"
                : "border border-border bg-surface-secondary text-muted",
            )}
          >
            {tileNumber}
          </span>
          <Terminal aria-hidden="true" className="size-3.5 shrink-0 text-muted" />
          <span className="truncate text-xs font-semibold text-foreground">{label}</span>
          <span className="shrink-0 font-mono text-[10.5px] text-meta">pid {pid}</span>
          <span
            className={cn(
              "inline-flex shrink-0 items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px] font-medium leading-none",
              statusPill.cls,
            )}
          >
            <span aria-hidden="true" className={cn("size-1.5 rounded-full", statusPill.dot)} />
            {statusPill.text}
          </span>
        </div>

        <div className="flex shrink-0 items-center gap-1">
          <button
            type="button"
            onClick={() => onOpenParentSession(sessionId)}
            aria-label="Open parent session"
            title="Open parent orchestrator session"
            className="inline-flex h-6 cursor-pointer items-center gap-1 rounded px-1.5 text-[11px] text-muted hover:bg-surface-secondary hover:text-foreground"
          >
            <ExternalLink aria-hidden="true" className="size-3" />
            <span className="hidden sm:inline">Parent</span>
          </button>
          <button
            type="button"
            onClick={() => void fetchTail()}
            aria-label="Refresh worker tail"
            title="Refresh output"
            className="inline-flex size-6 cursor-pointer items-center justify-center rounded text-muted hover:bg-surface-secondary hover:text-foreground"
          >
            <RefreshCw aria-hidden="true" className="size-3" />
          </button>
          <button
            type="button"
            onClick={onPromote}
            aria-label={`Promote tile ${tileNumber} to primary slot`}
            title="Move to primary slot"
            className="inline-flex size-6 cursor-pointer items-center justify-center rounded text-muted hover:bg-surface-secondary hover:text-foreground"
          >
            <ArrowUpLeft aria-hidden="true" className="size-3" />
          </button>
          <button
            type="button"
            onClick={onToggleMaximize}
            aria-label={maximized ? "Restore tile layout" : `Maximize tile ${tileNumber}`}
            title={maximized ? "Restore split (⌘⇧M)" : "Maximize tile (⌘⇧M)"}
            className="inline-flex size-6 cursor-pointer items-center justify-center rounded text-muted hover:bg-surface-secondary hover:text-foreground"
          >
            {maximized ? (
              <Minimize2 aria-hidden="true" className="size-3" />
            ) : (
              <Maximize2 aria-hidden="true" className="size-3" />
            )}
          </button>
          {canClose ? (
            <button
              type="button"
              onClick={onClose}
              aria-label={`Close tile ${tileNumber}`}
              title="Close tile"
              className="inline-flex size-6 cursor-pointer items-center justify-center rounded text-muted hover:bg-surface-secondary hover:text-foreground"
            >
              <X aria-hidden="true" className="size-3" />
            </button>
          ) : null}
        </div>
      </header>

      <div className="flex items-center justify-between gap-2 border-b border-separator/70 bg-surface-secondary/30 px-3 py-1.5">
        <code className="min-w-0 flex-1 truncate font-mono text-[11px] text-muted" title={command}>
          {command}
        </code>
        <div className="flex shrink-0 items-center gap-1.5">
          <button
            type="button"
            onClick={() => setAutoScroll((prev) => !prev)}
            aria-pressed={autoScroll}
            className={cn(
              "cursor-pointer rounded border px-1.5 py-0.5 text-[10px] font-medium transition-colors",
              autoScroll
                ? "border-sky-500/30 bg-sky-500/10 text-sky-500"
                : "border-border text-muted hover:text-foreground",
            )}
          >
            Auto-scroll {autoScroll ? "ON" : "OFF"}
          </button>
          <Button
            size="sm"
            variant="danger-soft"
            isDisabled={workerState !== "running" || killing}
            onPress={() => void handleKill()}
            aria-label="Kill worker"
            className="h-6 px-2 text-[11px]"
          >
            <Square aria-hidden="true" className="size-2.5" />
            Kill
          </Button>
        </div>
      </div>

      <pre
        ref={preRef}
        className="min-h-0 flex-1 overflow-auto bg-background p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap text-foreground/90"
      >
        {loading && !tail
          ? "Loading live output…"
          : tail || (workerState === "running" ? "(waiting for stdout…)" : "(no output captured)")}
      </pre>
    </section>
  );
}
