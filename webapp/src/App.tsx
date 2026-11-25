import * as React from "react";
import { HeaderBar } from "./components/HeaderBar";
import { BotsHistoryModal } from "./components/BotsHistoryModal";
import { CreateBotModal } from "./components/CreateBotModal";
import { CommandPalette } from "./components/CommandPalette";
import { FolderSidebar } from "./components/FolderSidebar";
import { SidebarDrawer } from "./components/SidebarDrawer";
import { FolderPicker } from "./components/FolderPicker";
import { SessionArchiveDialog } from "./components/SessionArchiveDialog";
import { SettingsView } from "./components/SettingsView";
import { McpView } from "./components/McpView";
import { AuthGate } from "./components/AuthGate";
import { TileGrid } from "./components/TileGrid";
import { TileLayoutSwitcher } from "./components/TileLayoutSwitcher";
import { SessionTile, type SessionTileHandle } from "./components/SessionTile";
import { WorkerTailTile } from "./components/WorkerTailTile";
import { GlobalJobsButton } from "./components/GlobalJobsButton";
import { authFetch } from "./lib/authToken";
import { apiUrl } from "./lib/serverUrl";
import { useCommandPalette } from "./lib/useCommandPalette";
import { useOpenUISystemPrompt, useRichUiMode } from "./lib/useRichUi";
import { useNotifications } from "./lib/useNotifications";
import { useSidebar } from "./lib/useSidebar";
import { useBots } from "./lib/useBots";
import { useTiling } from "./lib/useTiling";
import { displayTitle, EMPTY_SIDEBAR, type SessionActivity } from "./lib/sidebar";
import { useMediaQuery } from "./lib/useMediaQuery";
import { useOpenUIThemeMirror } from "./lib/theme";
import { useNativeShell } from "./lib/useNativeShell";
import { wantsRichUi } from "./lib/richUi";
import { initialState } from "./lib/transcript";
import type { ActiveSessionItem, ServerMessage } from "./protocol";
import type { BotConfig } from "./types/bots";

/** What the folder picker is being opened for. */
type PickerMode = "session" | "workspace";

export function App(): React.ReactElement {
  const [authState, setAuthState] = React.useState<"checking" | "required" | "ok">("checking");

  const checkAuth = React.useCallback(async () => {
    setAuthState("checking");
    try {
      const resp = await authFetch(apiUrl("/api/auth/status"), { credentials: "include" });
      if (resp.status === 404 || !resp.ok) {
        setAuthState("ok");
        return;
      }
      const data = (await resp.json()) as { authEnabled?: unknown; authenticated?: unknown };
      if (data.authEnabled === true && data.authenticated !== true) {
        setAuthState("required");
        return;
      }
      setAuthState("ok");
    } catch {
      // Older server / mock without /api/auth/status, or offline: fall through to WS.
      setAuthState("ok");
    }
  }, []);

  React.useEffect(() => {
    void checkAuth();
    const onChangeServer = (): void => {
      void checkAuth();
    };
    window.addEventListener("quartz:change-server", onChangeServer);
    return () => window.removeEventListener("quartz:change-server", onChangeServer);
  }, [checkAuth]);

  if (authState === "checking") {
    return (
      <div className="flex min-h-dvh items-center justify-center bg-background text-xs text-muted">
        Connecting…
      </div>
    );
  }
  if (authState === "required") {
    return <AuthGate onAuthenticated={() => setAuthState("ok")} />;
  }
  return <AuthenticatedApp onAuthError={() => setAuthState("required")} />;
}

function AuthenticatedApp({ onAuthError }: { onAuthError: () => void }): React.ReactElement {
  const richUi = useRichUiMode();
  const systemPromptAppend = useOpenUISystemPrompt(richUi.mode);
  const markNotificationsReadRef = React.useRef<() => void>(() => undefined);
  const notifications = useNotifications({
    onNotificationsRead: () => markNotificationsReadRef.current(),
  });

  const handleGlobalServerMessage = React.useCallback(
    (message: ServerMessage) => {
      if (message.type === "settings_state") {
        richUi.applySyncedMode(message.richUi ? "rich" : "markdown");
        void notifications.applySyncedEnabled(message.notifications, {
          turn: message.notifyTurn,
          approval: message.notifyApproval,
          background: message.notifyBackground,
          error: message.notifyError,
        });
        return;
      }
      if (message.type === "session_state" && message.settings) {
        richUi.applySyncedMode(message.settings.richUi ? "rich" : "markdown");
        void notifications.applySyncedEnabled(message.settings.notifications, {
          turn: message.settings.notifyTurn,
          approval: message.settings.notifyApproval,
          background: message.settings.notifyBackground,
          error: message.settings.notifyError,
        });
      }
      notifications.notifyFromFrame(message);
    },
    [richUi, notifications],
  );

  const sidebar = useSidebar();
  const bots = useBots();
  const [bannerHistoryBotId, setBannerHistoryBotId] = React.useState<string | null>(null);
  const [bannerEditBot, setBannerEditBot] = React.useState<BotConfig | null>(null);

  const [handles, setHandles] = React.useState<Readonly<Record<string, SessionTileHandle>>>({});
  const onHandleUpdate = React.useCallback((tileId: string, handle: SessionTileHandle) => {
    setHandles((prev) => (prev[tileId] === handle ? prev : { ...prev, [tileId]: handle }));
  }, []);
  const onHandleUnmount = React.useCallback((tileId: string) => {
    setHandles((prev) => {
      if (!(tileId in prev)) return prev;
      const next = { ...prev };
      delete next[tileId];
      return next;
    });
  }, []);

  const [picker, setPicker] = React.useState<PickerMode | null>(null);
  const [archiveOpen, setArchiveOpen] = React.useState(false);
  const [settingsOpen, setSettingsOpen] = React.useState(false);
  const [mcpOpen, setMcpOpen] = React.useState(false);
  const [sharedText, setSharedText] = React.useState<string | null>(null);

  const composerAnchorRef = React.useRef<HTMLTextAreaElement | null>(null);
  const palette = useCommandPalette(composerAnchorRef);

  const narrow = useMediaQuery("(max-width: 1023px)", false);
  const [sidebarOpen, setSidebarOpen] = React.useState(!narrow);
  React.useEffect(() => {
    setSidebarOpen(!narrow);
  }, [narrow]);
  const closeSidebar = React.useCallback(() => setSidebarOpen(false), []);

  useOpenUIThemeMirror();

  const overlayOpen =
    palette.open || picker !== null || archiveOpen || bannerEditBot !== null || bannerHistoryBotId !== null;

  const focusedHandleGuess = React.useMemo(() => {
    const vals = Object.values(handles);
    return vals[0];
  }, [handles]);

  const tiling = useTiling({
    hotkeysEnabled: !overlayOpen,
    defaultCwd: focusedHandleGuess?.session.cwd || undefined,
  });

  // First session tile owns global settings/notifications frames to prevent N× duplicates.
  const primarySessionTileId = React.useMemo(
    () => tiling.state.tiles.find((t) => t.kind === "session")?.id ?? null,
    [tiling.state.tiles],
  );

  const primaryHandle = primarySessionTileId ? handles[primarySessionTileId] : undefined;
  const focusedHandle = handles[tiling.state.focusedTileId] ?? primaryHandle;

  markNotificationsReadRef.current =
    primaryHandle?.markNotificationsRead ?? (() => undefined);

  const activeSession = focusedHandle?.session ?? initialState.session;
  const activeStatus = focusedHandle?.status ?? "connecting";
  const anyTurnRunning = React.useMemo(
    () => Object.values(handles).some((h) => h.turnRunning),
    [handles],
  );
  const totalBgCount = React.useMemo(() => {
    const seen = new Set<string>();
    let sum = 0;
    for (const h of Object.values(handles)) {
      const sid = h.session.sessionId || h.tileId;
      if (seen.has(sid)) continue;
      seen.add(sid);
      sum += h.session.bgCount ?? 0;
    }
    return sum;
  }, [handles]);

  const rawActiveSessions = primaryHandle?.activeSessions;
  React.useEffect(() => {
    void bots.reloadRuns();
  }, [rawActiveSessions, bots.reloadRuns]);

  const mergedActiveSessions = React.useMemo<readonly ActiveSessionItem[]>(() => {
    const base = [...(rawActiveSessions ?? [])];
    const seen = new Set(base.map((item) => item.sessionId));
    for (const h of Object.values(handles)) {
      const sid = h.session.sessionId;
      if (h.turnRunning && sid && !seen.has(sid)) {
        seen.add(sid);
        base.push({
          sessionId: sid,
          cwd: h.session.cwd,
          kind: "working",
          read: false,
          updatedAt: new Date().toISOString(),
        });
      }
    }
    return base;
  }, [rawActiveSessions, handles]);

  const mergedSessionActivity = React.useMemo<Readonly<Record<string, SessionActivity>>>(() => {
    const next: Record<string, SessionActivity> = {};
    for (const item of mergedActiveSessions) {
      next[item.sessionId] = {
        status: item.kind === "working" ? "running" : item.kind,
        unread: item.kind === "permission" ? true : !item.read,
      };
    }
    return next;
  }, [mergedActiveSessions]);

  const titleBySessionId = React.useMemo(() => {
    const map = new Map<string, string>();
    const sidebarState = sidebar.data?.sidebar ?? EMPTY_SIDEBAR;
    for (const s of sidebar.data?.sessions ?? []) {
      map.set(s.sessionId, displayTitle(s, sidebarState));
    }
    for (const item of mergedActiveSessions) {
      if (!map.has(item.sessionId) && item.title) {
        map.set(item.sessionId, item.title);
      }
    }
    return map;
  }, [sidebar.data, mergedActiveSessions]);

  const handleAndroidBack = React.useCallback((): boolean => {
    if (palette.open) {
      palette.onOpenChange(false);
      return true;
    }
    if (picker !== null) {
      setPicker(null);
      return true;
    }
    if (archiveOpen) {
      setArchiveOpen(false);
      return true;
    }
    if (settingsOpen) {
      setSettingsOpen(false);
      return true;
    }
    if (mcpOpen) {
      setMcpOpen(false);
      return true;
    }
    if (tiling.state.maximizedTileId !== null) {
      tiling.toggleMaximize(tiling.state.maximizedTileId);
      return true;
    }
    if (narrow && sidebarOpen) {
      setSidebarOpen(false);
      return true;
    }
    return false;
  }, [palette, picker, archiveOpen, settingsOpen, mcpOpen, tiling, narrow, sidebarOpen]);

  useNativeShell({
    turnRunning: anyTurnRunning,
    notificationsEnabled: notifications.enabled,
    onBack: handleAndroidBack,
    onSharedText: setSharedText,
  });

  const archiveOpener = React.useRef<HTMLElement | null>(null);
  const openArchive = React.useCallback(() => {
    const active = document.activeElement;
    archiveOpener.current = active instanceof HTMLElement ? active : null;
    setArchiveOpen(true);
  }, []);

  const selectSession = React.useCallback(
    (id: string) => {
      setSettingsOpen(false);
      setMcpOpen(false);
      tiling.selectSession(id);
      if (narrow) closeSidebar();
    },
    [tiling, narrow, closeSidebar],
  );

  const openSessionInNewTile = React.useCallback(
    (id: string) => {
      setSettingsOpen(false);
      setMcpOpen(false);
      tiling.openSessionInNewTile(id);
      if (narrow) closeSidebar();
    },
    [tiling, narrow, closeSidebar],
  );

  const newSessionIn = React.useCallback(
    (cwd: string) => {
      setSettingsOpen(false);
      setMcpOpen(false);
      const focusedTile = tiling.state.tiles.find((t) => t.id === tiling.state.focusedTileId);
      if (focusedTile?.kind === "session" && handles[focusedTile.id]) {
        handles[focusedTile.id]!.newSession(cwd);
      } else if (tiling.canAdd) {
        tiling.openNewTile(cwd);
      } else if (primaryHandle) {
        primaryHandle.newSession(cwd);
      }
      if (narrow) closeSidebar();
    },
    [tiling, handles, primaryHandle, narrow, closeSidebar],
  );

  const handlePaletteRun = React.useCallback(
    (name: string, arg?: string) => {
      const cleaned = name.trim().toLowerCase();
      if (cleaned === "settings" && (!arg || arg.trim() === "")) {
        setMcpOpen(false);
        setSettingsOpen(true);
        return;
      }
      if (cleaned === "mcp" && (!arg || arg.trim() === "")) {
        setSettingsOpen(false);
        setMcpOpen(true);
        return;
      }
      focusedHandle?.runCommand(name, arg);
    },
    [focusedHandle],
  );

  const sidebarProps = {
    data: sidebar.data,
    loading: sidebar.loading,
    error: sidebar.error,
    actionError: sidebar.actionError,
    currentSessionId: activeSession.sessionId,
    currentCwd: activeSession.cwd,
    onClose: closeSidebar,
    onReload: sidebar.reload,
    onSelectSession: selectSession,
    onOpenSessionInNewTile: openSessionInNewTile,
    tileBadges: tiling.tileBadges,
    canAddTile: tiling.canAdd,
    onNewSession: newSessionIn,
    onNewFolder: () => setPicker("workspace"),
    onRemoveFolder: sidebar.removeWorkspace,
    onOpenFolder: () => setPicker("session"),
    onRename: sidebar.rename,
    onHide: sidebar.hide,
    onRestore: sidebar.restore,
    onOpenArchive: openArchive,
    onOpenSettings: () => {
      setMcpOpen(false);
      setSettingsOpen(true);
    },
    onOpenMcp: () => {
      setSettingsOpen(false);
      setMcpOpen(true);
    },
    sessionActivity: mergedSessionActivity,
    activeSessions: mergedActiveSessions,
    onDismissActiveSession: primaryHandle?.dismissActiveSession,
    onClearReadActiveSessions: primaryHandle?.clearReadActiveSessions,
    bots: bots.bots,
    botRuns: bots.runs,
    togglingBotIds: bots.togglingIds,
    runningBotIds: bots.runningIds,
    botToggleError: bots.toggleError,
    onCreateBot: bots.create,
    onUpdateBot: bots.update,
    onToggleBot: bots.toggle,
    onRunBot: bots.runNow,
    onDeleteBot: bots.remove,
    onRestoreBot: bots.restore,
  };

  const multiTileActive = tiling.state.tiles.length > 1;
  const showFullWidthHeader =
    multiTileActive && !narrow && tiling.state.maximizedTileId === null;

  const tileControls = (
    <div className="flex items-center gap-1.5">
      <GlobalJobsButton
        bgCountHint={totalBgCount}
        onOpenWorkerTile={(job) => {
          setSettingsOpen(false);
          setMcpOpen(false);
          tiling.openWorkerInTile(job);
        }}
      />
      <TileLayoutSwitcher
        tiles={tiling.state.tiles}
        focusedTileId={tiling.state.focusedTileId}
        threeOrientation={tiling.state.threeOrientation}
        canAdd={tiling.canAdd}
        narrow={narrow}
        onSetCount={(count) => {
          setSettingsOpen(false);
          setMcpOpen(false);
          tiling.setTileCount(count, activeSession.cwd || undefined);
        }}
        onAddTile={() => {
          setSettingsOpen(false);
          setMcpOpen(false);
          tiling.openNewTile(activeSession.cwd || undefined);
        }}
        onFlipThree={tiling.flipThreeOrientation}
        onFocusTile={tiling.focusTile}
      />
    </div>
  );

  return (
    <div className="flex h-full min-h-0 bg-background text-foreground">
      {sidebarOpen && !narrow ? <FolderSidebar {...sidebarProps} variant="rail" /> : null}
      {sidebarOpen && narrow ? (
        <SidebarDrawer onClose={closeSidebar}>
          <FolderSidebar {...sidebarProps} variant="drawer" />
        </SidebarDrawer>
      ) : null}

      <div className="flex min-w-0 flex-1 flex-col">
        <HeaderBar
          session={activeSession}
          status={activeStatus}
          richUiMode={richUi.mode}
          onRichUiModeChange={richUi.setMode}
          onNewSession={() => {
            if (activeSession.cwd) newSessionIn(activeSession.cwd);
            else setPicker("session");
          }}
          onChangeFolder={() => setPicker("session")}
          onToggleSandbox={
            focusedHandle
              ? () => focusedHandle.setSandbox(!activeSession.sandboxed)
              : undefined
          }
          turnRunning={focusedHandle?.turnRunning ?? false}
          sidebarOpen={sidebarOpen}
          onToggleSidebar={() => setSidebarOpen((open) => !open)}
          fullWidth={showFullWidthHeader}
          tileControls={tileControls}
        />

        {settingsOpen ? (
          <SettingsView
            onBack={() => setSettingsOpen(false)}
            notifications={notifications}
          />
        ) : mcpOpen ? (
          <McpView onBack={() => setMcpOpen(false)} />
        ) : (
          <TileGrid
            tiles={tiling.state.tiles}
            focusedTileId={tiling.state.focusedTileId}
            maximizedTileId={tiling.state.maximizedTileId}
            threeOrientation={tiling.state.threeOrientation}
            ratios={tiling.state.ratios}
            narrow={narrow}
            onRatioChange={tiling.setRatio}
            onResetRatios={tiling.resetRatios}
            renderTile={(tile, index) => {
              const tileNumber = index + 1;
              const focused = tile.id === tiling.state.focusedTileId;
              const maximized = tile.id === tiling.state.maximizedTileId;
              const canClose = tiling.state.tiles.length > 1;

              if (tile.kind === "worker") {
                return (
                  <WorkerTailTile
                    tileNumber={tileNumber}
                    focused={focused}
                    maximized={maximized}
                    canClose={canClose}
                    sessionId={tile.sessionId}
                    pid={tile.pid}
                    label={tile.label}
                    command={tile.command}
                    onFocus={() => tiling.focusTile(tile.id)}
                    onClose={() => tiling.closeTile(tile.id)}
                    onToggleMaximize={() => tiling.toggleMaximize(tile.id)}
                    onPromote={() => tiling.promoteTile(tile.id)}
                    onOpenParentSession={(sid) => tiling.openSessionInNewTile(sid)}
                  />
                );
              }

              const resolvedTitle = tile.sessionId
                ? titleBySessionId.get(tile.sessionId)
                : undefined;
              return (
                <SessionTile
                  tileId={tile.id}
                  tileNumber={tileNumber}
                  multiTile={multiTileActive}
                  focused={focused}
                  maximized={maximized}
                  canClose={canClose}
                  isPrimary={tile.id === primarySessionTileId}
                  requestedSessionId={tile.sessionId}
                  initialCwd={tile.initialCwd}
                  createFreshOnConnect={tile.sessionId === null && tile.id !== primarySessionTileId}
                  systemPromptAppend={systemPromptAppend}
                  richUi={wantsRichUi(richUi.mode)}
                  sessionTitle={resolvedTitle}
                  bots={bots.bots}
                  botRuns={bots.runs}
                  runningBotIds={bots.runningIds}
                  sharedText={sharedText}
                  onSharedTextConsumed={() => setSharedText(null)}
                  onAuthError={onAuthError}
                  onGlobalServerMessage={handleGlobalServerMessage}
                  onSessionBound={tiling.bindSessionId}
                  onHandleUpdate={onHandleUpdate}
                  onHandleUnmount={onHandleUnmount}
                  onReloadSidebar={sidebar.reload}
                  onFocus={() => tiling.focusTile(tile.id)}
                  onClose={() => tiling.closeTile(tile.id)}
                  onToggleMaximize={() => tiling.toggleMaximize(tile.id)}
                  onPromote={() => tiling.promoteTile(tile.id)}
                  onOpenSettings={() => {
                    setMcpOpen(false);
                    setSettingsOpen(true);
                  }}
                  onOpenMcp={() => {
                    setSettingsOpen(false);
                    setMcpOpen(true);
                  }}
                  onOpenPalette={palette.openPalette}
                  onOpenBotHistory={(botId) => setBannerHistoryBotId(botId)}
                  onEditBot={(bot) => setBannerEditBot(bot)}
                  onRerunBot={(botId) => {
                    void bots.runNow(botId);
                  }}
                  onOpenWorkerTile={(job) => tiling.openWorkerInTile(job)}
                />
              );
            }}
          />
        )}
      </div>

      <CommandPalette
        open={palette.open}
        commands={activeSession.commands}
        onOpenChange={palette.onOpenChange}
        onRun={handlePaletteRun}
      />

      <FolderPicker
        open={picker !== null}
        onOpenChange={(open) => {
          if (!open) setPicker(null);
        }}
        title={picker === "workspace" ? "Add a folder" : "New session"}
        chooseLabel={picker === "workspace" ? "Add to sidebar" : "Start session"}
        onChoose={(path) => {
          if (picker === "workspace") void sidebar.addWorkspace(path);
          else newSessionIn(path);
        }}
      />

      <CreateBotModal
        open={bannerEditBot !== null}
        initialBot={bannerEditBot}
        defaultCwd={activeSession.cwd}
        onClose={() => setBannerEditBot(null)}
        onSubmit={async (payload) => {
          if (bannerEditBot) await bots.update(bannerEditBot.id, payload);
        }}
      />

      <BotsHistoryModal
        open={bannerHistoryBotId !== null}
        bots={bots.bots}
        initialBotId={bannerHistoryBotId ?? undefined}
        onClose={() => setBannerHistoryBotId(null)}
        onOpenSession={selectSession}
        onRestoreBot={bots.restore}
      />

      <SessionArchiveDialog
        open={archiveOpen}
        onOpenChange={setArchiveOpen}
        data={sidebar.data}
        loading={sidebar.loading}
        error={sidebar.error}
        actionError={sidebar.actionError}
        onReload={sidebar.reload}
        onImport={sidebar.importSession}
        onRestore={sidebar.restore}
        returnFocusTo={archiveOpener}
      />
    </div>
  );
}
