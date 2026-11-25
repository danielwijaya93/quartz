import * as React from "react";
import { cn } from "@heroui/react";
import {
  ArrowUpLeft,
  Folder,
  GitBranch,
  Maximize2,
  Minimize2,
  Terminal,
  X,
} from "lucide-react";
import { BotRunBanner } from "./BotRunBanner";
import { Transcript } from "./Transcript";
import { StatusLine } from "./StatusLine";
import { JobsTray } from "./JobsTray";
import { PermissionPromptCard } from "./PermissionPromptCard";
import { Composer } from "./Composer";
import { TrustFolderDialog } from "./TrustFolderDialog";
import { ModelPickerDialog } from "./ModelPickerDialog";
import { useQuartz } from "@/useQuartz";
import { directoryLabel, type SessionActivity } from "@/lib/sidebar";
import type { ConnectionStatus } from "@/lib/connection";
import type { SessionMeta } from "@/lib/transcript";
import type { ActiveSessionItem, ImageAttachment, ServerMessage } from "@/protocol";
import type { BotConfig, BotRunRecord } from "@/types/bots";

export interface SessionTileHandle {
  tileId: string;
  session: SessionMeta;
  status: ConnectionStatus;
  turnRunning: boolean;
  activeSessions?: readonly ActiveSessionItem[];
  activity?: SessionActivity;
  loadSession: (sessionId: string) => void;
  newSession: (cwd?: string) => void;
  setSandbox: (enabled: boolean) => void;
  runCommand: (name: string, arg?: string) => void;
  dismissActiveSession: (sessionId: string) => void;
  clearReadActiveSessions: () => void;
  markNotificationsRead: () => void;
  focusComposer: () => void;
  openJobs: () => void;
  openModelPicker: () => void;
}

export interface SessionTileProps {
  tileId: string;
  tileNumber: number;
  multiTile: boolean;
  focused: boolean;
  maximized: boolean;
  canClose: boolean;
  /** Only one primary tile subscribes to global notifications/settings frames. */
  isPrimary: boolean;
  requestedSessionId: string | null;
  initialCwd?: string;
  createFreshOnConnect: boolean;
  systemPromptAppend: string;
  richUi: boolean;
  sessionTitle?: string;
  bots: readonly BotConfig[];
  botRuns: readonly BotRunRecord[];
  runningBotIds: ReadonlySet<string>;
  sharedText: string | null;
  onSharedTextConsumed: () => void;
  onAuthError: () => void;
  onGlobalServerMessage?: (message: ServerMessage) => void;
  onSessionBound: (tileId: string, sessionId: string) => void;
  onHandleUpdate: (tileId: string, handle: SessionTileHandle) => void;
  onHandleUnmount: (tileId: string) => void;
  onReloadSidebar: () => void;
  onFocus: () => void;
  onClose: () => void;
  onToggleMaximize: () => void;
  onPromote: () => void;
  onOpenSettings: () => void;
  onOpenMcp: () => void;
  onOpenPalette: () => void;
  onOpenBotHistory: (botId: string) => void;
  onEditBot: (bot: BotConfig) => void;
  onRerunBot: (botId: string) => void;
  onOpenWorkerTile: (job: {
    sessionId: string;
    pid: string;
    label: string;
    command: string;
  }) => void;
}

export function SessionTile({
  tileId,
  tileNumber,
  multiTile,
  focused,
  maximized,
  canClose,
  isPrimary,
  requestedSessionId,
  initialCwd,
  createFreshOnConnect,
  systemPromptAppend,
  richUi,
  sessionTitle,
  bots,
  botRuns,
  runningBotIds,
  sharedText,
  onSharedTextConsumed,
  onAuthError,
  onGlobalServerMessage,
  onSessionBound,
  onHandleUpdate,
  onHandleUnmount,
  onReloadSidebar,
  onFocus,
  onClose,
  onToggleMaximize,
  onPromote,
  onOpenSettings,
  onOpenMcp,
  onOpenPalette,
  onOpenBotHistory,
  onEditBot,
  onRerunBot,
  onOpenWorkerTile,
}: SessionTileProps): React.ReactElement {
  const composerRef = React.useRef<HTMLTextAreaElement | null>(null);
  const [jobsOpen, setJobsOpen] = React.useState(false);
  const [modelPickerOpen, setModelPickerOpen] = React.useState(false);
  const [dismissedTrustCwds, setDismissedTrustCwds] = React.useState<ReadonlySet<string>>(
    () => new Set(),
  );

  const onMessage = React.useCallback(
    (msg: ServerMessage) => {
      if (isPrimary) {
        onGlobalServerMessage?.(msg);
      }
    },
    [isPrimary, onGlobalServerMessage],
  );

  const quartz = useQuartz(
    undefined,
    systemPromptAppend,
    onMessage,
    onAuthError,
    {
      initialSessionId: requestedSessionId,
      createFreshCwd: createFreshOnConnect ? (initialCwd ?? "") : undefined,
    },
  );

  const sessionId = quartz.state.session.sessionId;
  const lastRequestedRef = React.useRef<string | null>(requestedSessionId);

  // When the user selects a different session in the sidebar for this tile, load it.
  React.useEffect(() => {
    if (
      requestedSessionId &&
      requestedSessionId !== lastRequestedRef.current &&
      requestedSessionId !== sessionId
    ) {
      lastRequestedRef.current = requestedSessionId;
      quartz.loadSession(requestedSessionId);
    } else if (requestedSessionId === null) {
      lastRequestedRef.current = null;
    }
  }, [requestedSessionId, sessionId, quartz]);

  // Report the bound sessionId back to the tiling store once `session_state` arrives.
  React.useEffect(() => {
    if (sessionId) {
      lastRequestedRef.current = sessionId;
      onSessionBound(tileId, sessionId);
      onReloadSidebar();
    }
  }, [tileId, sessionId, onSessionBound, onReloadSidebar]);

  // Reload sidebar on turn start/end transitions.
  const wasRunning = React.useRef(false);
  React.useEffect(() => {
    if (wasRunning.current !== quartz.state.turnRunning) {
      onReloadSidebar();
    }
    wasRunning.current = quartz.state.turnRunning;
  }, [quartz.state.turnRunning, onReloadSidebar]);

  // Focus composer when tile becomes focused via shortcut (avoid stealing from inputs).
  React.useEffect(() => {
    if (!focused || !multiTile) return;
    const active = document.activeElement;
    if (active && active !== document.body && active.tagName === "INPUT") return;
    composerRef.current?.focus();
  }, [focused, multiTile]);

  const activeBotRun = React.useMemo(
    () => (sessionId ? botRuns.find((r) => r.session_id === sessionId) : undefined),
    [botRuns, sessionId],
  );
  const activeBotConfig = React.useMemo(
    () => (activeBotRun ? bots.find((b) => b.id === activeBotRun.bot_id) : undefined),
    [activeBotRun, bots],
  );

  const projectTrust = quartz.state.session.projectTrust;
  const trustPromptOpen = Boolean(
    projectTrust?.needsTrust && !dismissedTrustCwds.has(projectTrust.cwd),
  );

  const onSubmit = React.useCallback(
    (text: string, images: ImageAttachment[]) => {
      quartz.prompt(text, images);
    },
    [quartz],
  );

  const onPrompt = React.useCallback(
    (text: string) => {
      quartz.prompt(text, []);
    },
    [quartz],
  );

  const handleRunCommand = React.useCallback(
    (name: string, arg?: string) => {
      const cleaned = name.trim().toLowerCase();
      if (cleaned === "model" && (!arg || arg.trim() === "")) {
        setModelPickerOpen(true);
        return;
      }
      if (cleaned === "settings" && (!arg || arg.trim() === "")) {
        onOpenSettings();
        return;
      }
      if (cleaned === "mcp" && (!arg || arg.trim() === "")) {
        onOpenMcp();
        return;
      }
      if (cleaned === "jobs" && (!arg || arg.trim() === "")) {
        quartz.clearBgFailed();
        setJobsOpen(true);
        return;
      }
      quartz.runCommand(name, arg);
    },
    [quartz, onOpenSettings, onOpenMcp],
  );

  const handle = React.useMemo<SessionTileHandle>(
    () => ({
      tileId,
      session: quartz.state.session,
      status: quartz.status,
      turnRunning: quartz.state.turnRunning,
      activeSessions: quartz.state.activeSessions,
      loadSession: quartz.loadSession,
      newSession: quartz.newSession,
      setSandbox: quartz.setSandbox,
      runCommand: handleRunCommand,
      dismissActiveSession: quartz.dismissActiveSession,
      clearReadActiveSessions: quartz.clearReadActiveSessions,
      markNotificationsRead: quartz.markNotificationsRead,
      focusComposer: () => composerRef.current?.focus(),
      openJobs: () => {
        quartz.clearBgFailed();
        setJobsOpen(true);
      },
      openModelPicker: () => setModelPickerOpen(true),
    }),
    [tileId, quartz, handleRunCommand],
  );

  React.useEffect(() => {
    onHandleUpdate(tileId, handle);
  }, [tileId, handle, onHandleUpdate]);

  React.useEffect(() => {
    return () => onHandleUnmount(tileId);
  }, [tileId, onHandleUnmount]);

  const hasPermission = Boolean(quartz.state.session.pendingPermission);
  const bgCount = quartz.state.session.bgCount ?? 0;
  const statusDotClass = hasPermission
    ? "bg-orange-500"
    : quartz.state.turnRunning
      ? "bg-sky-500 motion-safe:animate-pulse"
      : bgCount > 0
        ? "bg-amber-500"
        : quartz.status === "connected"
          ? "bg-emerald-500"
          : "bg-muted";

  const displayTileTitle =
    sessionTitle ||
    (activeBotConfig ? activeBotConfig.name : "") ||
    (quartz.state.session.cwd ? directoryLabel(quartz.state.session.cwd) : "New session");

  return (
    <section
      aria-label={multiTile ? `Tile ${tileNumber}: ${displayTileTitle}` : "Session workspace"}
      onMouseDown={onFocus}
      className={cn(
        "flex h-full min-h-0 flex-1 flex-col overflow-hidden bg-background",
        multiTile && "rounded-xl border transition-colors",
        multiTile &&
          (hasPermission
            ? "border-orange-500/50"
            : focused
              ? "border-sky-500/50 ring-1 ring-sky-500/20"
              : "border-border/80"),
      )}
    >
      {multiTile ? (
        <header
          className={cn(
            "flex h-9 shrink-0 items-center justify-between gap-2 border-b border-separator px-2.5",
            hasPermission
              ? "bg-orange-500/5"
              : focused
                ? "bg-sky-500/5"
                : "bg-surface/70",
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
            <span aria-hidden="true" className={cn("size-1.5 shrink-0 rounded-full", statusDotClass)} />
            <span className="min-w-0 truncate text-xs font-semibold text-foreground" title={displayTileTitle}>
              {displayTileTitle}
            </span>
            {quartz.state.session.cwd ? (
              <span className="hidden shrink-0 items-center gap-1 rounded-full border border-border bg-surface-secondary/60 px-1.5 py-0.5 text-[10px] text-muted md:inline-flex">
                <Folder aria-hidden="true" className="size-2.5" />
                <span className="max-w-24 truncate">{directoryLabel(quartz.state.session.cwd)}</span>
              </span>
            ) : null}
            {quartz.state.session.gitBranch ? (
              <span className="hidden shrink-0 items-center gap-1 rounded-full border border-border bg-surface-secondary/40 px-1.5 py-0.5 font-mono text-[10px] text-muted xl:inline-flex">
                <GitBranch aria-hidden="true" className="size-2.5" />
                <span className="max-w-24 truncate">{quartz.state.session.gitBranch}</span>
              </span>
            ) : null}
          </div>

          <div className="flex shrink-0 items-center gap-1">
            {bgCount > 0 ? (
              <button
                type="button"
                onClick={() => {
                  quartz.clearBgFailed();
                  setJobsOpen((prev) => !prev);
                }}
                aria-label={`${bgCount} background jobs in tile ${tileNumber}`}
                className="inline-flex h-5 cursor-pointer items-center gap-1 rounded-full border border-sky-500/30 bg-sky-500/10 px-1.5 text-[10px] font-medium text-sky-600 hover:bg-sky-500/20 dark:text-sky-400"
              >
                <Terminal aria-hidden="true" className="size-2.5" />
                <span className="tabular-nums">{bgCount}</span>
              </button>
            ) : null}
            <button
              type="button"
              onClick={onPromote}
              aria-label={`Promote tile ${tileNumber} to primary slot`}
              title="Swap into primary slot"
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
      ) : null}

      {activeBotRun ? (
        <BotRunBanner
          run={activeBotRun}
          bot={activeBotConfig}
          isRunning={runningBotIds.has(activeBotRun.bot_id)}
          onOpenHistory={onOpenBotHistory}
          onEditBot={onEditBot}
          onRerun={onRerunBot}
        />
      ) : null}

      <Transcript
        rows={quartz.state.rows}
        turnRunning={quartz.state.turnRunning}
        cwd={quartz.state.session.cwd}
        richUi={richUi}
        onPrompt={onPrompt}
        onFormState={quartz.setFormState}
        onRunCommand={handleRunCommand}
      />

      <StatusLine
        label={quartz.state.status}
        status={quartz.status}
        turnRunning={quartz.state.turnRunning}
      />

      <JobsTray
        open={jobsOpen}
        sessionId={sessionId || undefined}
        onOpenInTile={
          sessionId
            ? (job) => {
                setJobsOpen(false);
                onOpenWorkerTile({ sessionId, ...job });
              }
            : undefined
        }
        onClose={() => setJobsOpen(false)}
      />

      {(quartz.state.idlePrompts ?? []).map((prompt) => {
        const hours = Math.max(1, Math.round(prompt.idleSeconds / 3600));
        const folder = prompt.cwd.split("/").filter(Boolean).pop() || prompt.cwd;
        return (
          <div
            key={prompt.sessionId}
            role="region"
            aria-label="Idle session prompt"
            className="mx-auto mb-2 flex w-full max-w-3xl flex-wrap items-center justify-between gap-2 rounded-lg border border-border bg-content1 px-3 py-2 text-xs shadow-sm"
          >
            <span className="text-foreground/90">
              Session <code className="font-mono">{prompt.sessionId.slice(0, 12)}</code> (
              {folder}) has been idle for {hours}h. Keep it alive or close it?
            </span>
            <div className="flex items-center gap-2">
              <button
                type="button"
                className="rounded border border-border px-2.5 py-1 text-xs font-medium hover:bg-content2"
                onClick={() => quartz.respondIdleSession(prompt.sessionId, "keep")}
              >
                Keep alive
              </button>
              <button
                type="button"
                className="rounded bg-danger/15 px-2.5 py-1 text-xs font-medium text-danger hover:bg-danger/25"
                onClick={() => quartz.respondIdleSession(prompt.sessionId, "close")}
              >
                Close session
              </button>
            </div>
          </div>
        );
      })}

      {quartz.state.session.pendingPermission ? (
        <PermissionPromptCard
          permission={quartz.state.session.pendingPermission}
          onRespond={quartz.respondPermission}
        />
      ) : null}

      <Composer
        connection={quartz.status}
        cwd={quartz.state.session.cwd}
        turnRunning={quartz.state.turnRunning}
        fatalError={quartz.state.fatalError}
        commands={quartz.state.session.commands}
        model={quartz.state.session.model}
        effort={quartz.state.session.effort}
        contextPct={quartz.state.session.contextPct}
        maxContextTokens={quartz.state.session.maxContextTokens}
        cacheHitPct={quartz.state.session.cacheHitPct}
        bgCount={quartz.state.session.bgCount}
        bgFailed={quartz.state.session.bgFailed}
        onOpenJobs={() => {
          quartz.clearBgFailed();
          setJobsOpen((prev) => !prev);
        }}
        onOpenModelPicker={() => setModelPickerOpen(true)}
        onSubmit={onSubmit}
        onSteer={quartz.steer}
        onCancel={quartz.cancel}
        onRunCommand={handleRunCommand}
        onOpenPalette={onOpenPalette}
        sharedText={focused ? sharedText : null}
        onSharedTextConsumed={onSharedTextConsumed}
        textareaRef={composerRef}
      />

      <TrustFolderDialog
        open={trustPromptOpen}
        cwd={projectTrust?.cwd ?? ""}
        files={projectTrust?.files ?? []}
        onTrust={() => quartz.trustProject()}
        onDismiss={() => {
          if (!projectTrust) return;
          setDismissedTrustCwds((prev) => new Set(prev).add(projectTrust.cwd));
        }}
      />

      <ModelPickerDialog
        open={modelPickerOpen}
        onOpenChange={setModelPickerOpen}
        currentModel={quartz.state.session.model}
        currentEffort={quartz.state.session.effort}
      />
    </section>
  );
}
