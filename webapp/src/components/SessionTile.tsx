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
import { useOpenUISystemPrompt } from "@/lib/useRichUi";
import { directoryLabel, type SessionActivity } from "@/lib/sidebar";
import type { ConnectionStatus } from "@/lib/connection";
import type { SessionMeta } from "@/lib/transcript";
import type { ActiveSessionItem, ImageAttachment, ServerMessage, WatcherAction } from "@/protocol";
import type { WatcherConfigUpdate } from "./WatcherControl";
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
  setRichUi: (enabled: boolean) => void;
  runCommand: (name: string, arg?: string) => void;
  dismissActiveSession: (sessionId: string) => void;
  clearReadActiveSessions: () => void;
  markNotificationsRead: () => void;
  focusComposer: () => void;
  openJobs: () => void;
  openModelPicker: () => void;
  configureWatcher: (config: WatcherConfigUpdate) => void;
  watcherAction: (action: WatcherAction) => void;
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
  onFocus: (tileId: string) => void;
  onClose: (tileId: string) => void;
  onToggleMaximize: (tileId: string) => void;
  onPromote: (tileId: string) => void;
  onOpenSettings: () => void;
  onOpenMcp: () => void;
  onOpenPalette: () => void;
  onOpenBotHistory: (botId: string) => void;
  onEditBot: (bot: BotConfig) => void;
  onRerunBot: (botId: string) => void;
}

export const SessionTile = React.memo(function SessionTile({
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
}: SessionTileProps): React.ReactElement {
  const composerRef = React.useRef<HTMLTextAreaElement | null>(null);
  const [tileRichUi, setTileRichUi] = React.useState(false);
  const systemPromptAppend = useOpenUISystemPrompt(tileRichUi ? "rich" : "markdown");
  const [jobsOpen, setJobsOpen] = React.useState(false);
  const [modelPickerOpen, setModelPickerOpen] = React.useState(false);
  const [dismissedTrustCwds, setDismissedTrustCwds] = React.useState<ReadonlySet<string>>(
    () => new Set(),
  );

  const onMessage = React.useCallback(
    (msg: ServerMessage) => {
      // Forward active_sessions_state from every tile so closing tile 1 never
      // loses the app-level Active Sessions list; all other global frames stay
      // owned by the primary tile to avoid N× duplicate notifications.
      if (
        isPrimary ||
        msg.type === "active_sessions_state" ||
        msg.type === "sidebar_updated"
      ) {
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

  const {
    state: qState,
    status: qStatus,
    prompt: qPrompt,
    steer: qSteer,
    cancel: qCancel,
    runCommand: qRunCommand,
    newSession: qNewSession,
    loadSession: qLoadSession,
    trustProject: qTrustProject,
    setSandbox: qSetSandbox,
    setRichUi: qSetRichUi,
    respondPermission: qRespondPermission,
    setFormState: qSetFormState,
    clearBgFailed: qClearBgFailed,
    dismissActiveSession: qDismissActiveSession,
    clearReadActiveSessions: qClearReadActiveSessions,
    markNotificationsRead: qMarkNotificationsRead,
  } = quartz;

  const sessionId = qState.session.sessionId;
  const lastRequestedRef = React.useRef<string | null>(requestedSessionId);

  React.useEffect(() => {
    if (typeof qState.session.richUi === "boolean") {
      setTileRichUi(qState.session.richUi);
    }
  }, [qState.session.richUi]);

  const handleSetRichUi = React.useCallback(
    (enabled: boolean) => {
      setTileRichUi(enabled);
      if (!enabled) {
        qSetRichUi(false, "");
        return;
      }
      void import("@/lib/openui/prompt")
        .then((module) => {
          qSetRichUi(true, module.getOpenUISystemPrompt());
        })
        .catch(() => {
          qSetRichUi(true, "");
        });
    },
    [qSetRichUi],
  );

  // When the user selects a different session in the sidebar for this tile, load it.
  React.useEffect(() => {
    if (
      requestedSessionId &&
      requestedSessionId !== lastRequestedRef.current &&
      requestedSessionId !== sessionId
    ) {
      lastRequestedRef.current = requestedSessionId;
      qLoadSession(requestedSessionId);
    } else if (requestedSessionId === null) {
      lastRequestedRef.current = null;
    }
  }, [requestedSessionId, sessionId, qLoadSession]);

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
    if (wasRunning.current !== qState.turnRunning) {
      onReloadSidebar();
    }
    wasRunning.current = qState.turnRunning;
  }, [qState.turnRunning, onReloadSidebar]);

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
      qPrompt(text, images);
    },
    [qPrompt],
  );

  const onPrompt = React.useCallback(
    (text: string) => {
      qPrompt(text, []);
    },
    [qPrompt],
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
        qClearBgFailed();
        setJobsOpen(true);
        return;
      }
      qRunCommand(name, arg);
    },
    [qClearBgFailed, qRunCommand, onOpenSettings, onOpenMcp],
  );

  // Depend ONLY on coarse session metadata and stable callbacks — never on
  // `qState.rows` or the full `quartz` object — so streaming tokens in one tile
  // never bubble state updates up to App and re-render sibling tiles or sidebar.
  const handle = React.useMemo<SessionTileHandle>(
    () => ({
      tileId,
      session: qState.session,
      status: qStatus,
      turnRunning: qState.turnRunning,
      activeSessions: qState.activeSessions,
      loadSession: qLoadSession,
      newSession: qNewSession,
      setSandbox: qSetSandbox,
      setRichUi: handleSetRichUi,
      runCommand: handleRunCommand,
      dismissActiveSession: qDismissActiveSession,
      clearReadActiveSessions: qClearReadActiveSessions,
      markNotificationsRead: qMarkNotificationsRead,
      focusComposer: () => composerRef.current?.focus(),
      openJobs: () => {
        qClearBgFailed();
        setJobsOpen(true);
      },
      openModelPicker: () => setModelPickerOpen(true),
      configureWatcher: quartz.configureWatcher,
      watcherAction: quartz.watcherAction,
    }),
    [
      tileId,
      qState.session,
      qStatus,
      qState.turnRunning,
      qState.activeSessions,
      qLoadSession,
      qNewSession,
      qSetSandbox,
      handleSetRichUi,
      handleRunCommand,
      qDismissActiveSession,
      qClearReadActiveSessions,
      qMarkNotificationsRead,
      qClearBgFailed,
      quartz.configureWatcher,
      quartz.watcherAction,
    ],
  );

  const handleMouseDown = React.useCallback(() => {
    if (!focused) onFocus(tileId);
  }, [focused, onFocus, tileId]);

  const handleClose = React.useCallback(() => onClose(tileId), [onClose, tileId]);
  const handleToggleMaximize = React.useCallback(
    () => onToggleMaximize(tileId),
    [onToggleMaximize, tileId],
  );
  const handlePromote = React.useCallback(() => onPromote(tileId), [onPromote, tileId]);
  const handleToggleJobs = React.useCallback(() => {
    qClearBgFailed();
    setJobsOpen((prev) => !prev);
  }, [qClearBgFailed]);
  const handleCloseJobs = React.useCallback(() => setJobsOpen(false), []);
  const handleOpenModelPicker = React.useCallback(() => setModelPickerOpen(true), []);
  const handleTrustProject = React.useCallback(() => qTrustProject(), [qTrustProject]);

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
      ? "bg-accent motion-safe:animate-pulse"
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
      onMouseDown={handleMouseDown}
      className={cn(
        "flex h-full min-h-0 flex-1 flex-col overflow-hidden bg-background",
        multiTile && "rounded-xl border transition-colors",
        multiTile &&
          (hasPermission
            ? "border-orange-500/50"
            : focused
              ? "border-accent/40 ring-1 ring-accent/20"
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
                ? "bg-accent-soft/40"
                : "bg-surface/70",
          )}
        >
          <div className="flex min-w-0 items-center gap-1.5">
            <span
              className={cn(
                "inline-flex size-4 shrink-0 items-center justify-center rounded text-[10px] font-bold tabular-nums",
                focused
                  ? "bg-accent text-accent-foreground"
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
                className="inline-flex h-5 cursor-pointer items-center gap-1 rounded-full border border-border bg-surface-secondary/60 px-1.5 text-[10px] font-medium text-foreground hover:bg-surface-secondary"
              >
                <Terminal aria-hidden="true" className="size-2.5" />
                <span className="tabular-nums">{bgCount}</span>
              </button>
            ) : null}
            <button
              type="button"
              onClick={handlePromote}
              aria-label={`Promote tile ${tileNumber} to primary slot`}
              title="Swap into primary slot"
              className="inline-flex size-6 cursor-pointer items-center justify-center rounded text-muted hover:bg-surface-secondary hover:text-foreground"
            >
              <ArrowUpLeft aria-hidden="true" className="size-3" />
            </button>
            <button
              type="button"
              onClick={handleToggleMaximize}
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
                onClick={handleClose}
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
        rows={qState.rows}
        turnRunning={qState.turnRunning}
        cwd={qState.session.cwd}
        richUi={tileRichUi}
        onPrompt={onPrompt}
        onFormState={qSetFormState}
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
        onClose={handleCloseJobs}
      />

      {quartz.state.session.pendingPermission ? (
        <PermissionPromptCard
          permission={quartz.state.session.pendingPermission}
          onRespond={qRespondPermission}
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
        totalCostUsd={quartz.state.session.totalCostUsd}
        bgCount={quartz.state.session.bgCount}
        bgFailed={quartz.state.session.bgFailed}
        onOpenJobs={handleToggleJobs}
        onOpenModelPicker={handleOpenModelPicker}
        onSubmit={onSubmit}
        onSteer={qSteer}
        onCancel={qCancel}
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
        onTrust={handleTrustProject}
        onDismiss={() => {
          if (!projectTrust) return;
          setDismissedTrustCwds((prev) => new Set(prev).add(projectTrust.cwd));
        }}
      />

      <ModelPickerDialog
        open={modelPickerOpen}
        onOpenChange={setModelPickerOpen}
        sessionId={sessionId || undefined}
        currentModel={qState.session.model}
        currentEffort={qState.session.effort}
      />
    </section>
  );
});
