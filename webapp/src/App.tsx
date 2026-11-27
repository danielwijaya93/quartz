import * as React from "react";
import { cn } from "@heroui/react";
import { HeaderBar } from "./components/HeaderBar";
import { CommandPalette } from "./components/CommandPalette";
import { FolderSidebar } from "./components/FolderSidebar";
import { SidebarDrawer } from "./components/SidebarDrawer";
import { AuthGate } from "./components/AuthGate";

const BotsHistoryModal = React.lazy(() =>
  import("./components/BotsHistoryModal").then((m) => ({ default: m.BotsHistoryModal })),
);
const CreateBotModal = React.lazy(() =>
  import("./components/CreateBotModal").then((m) => ({ default: m.CreateBotModal })),
);
const FolderPicker = React.lazy(() =>
  import("./components/FolderPicker").then((m) => ({ default: m.FolderPicker })),
);
const SessionArchiveDialog = React.lazy(() =>
  import("./components/SessionArchiveDialog").then((m) => ({ default: m.SessionArchiveDialog })),
);
const SettingsView = React.lazy(() =>
  import("./components/SettingsView").then((m) => ({ default: m.SettingsView })),
);
const McpView = React.lazy(() =>
  import("./components/McpView").then((m) => ({ default: m.McpView })),
);
import { TileGrid } from "./components/TileGrid";
import { MobileTileBar, TileLayoutSwitcher } from "./components/TileLayoutSwitcher";
import { SessionTile, type SessionTileHandle } from "./components/SessionTile";
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

function sameActiveSessions(
  a: readonly ActiveSessionItem[],
  b: readonly ActiveSessionItem[],
): boolean {
  if (a === b) return true;
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i += 1) {
    const x = a[i]!;
    const y = b[i]!;
    if (
      x.sessionId !== y.sessionId ||
      x.kind !== y.kind ||
      x.read !== y.read ||
      x.title !== y.title ||
      x.preview !== y.preview ||
      x.isBot !== y.isBot
    ) {
      return false;
    }
  }
  return true;
}

export function App(): React.ReactElement {
  // Optimistically mount the workspace so WebSocket + sidebar requests start
  // immediately instead of waiting a full RTT on /api/auth/status; both the
  // parallel auth check and WS_AUTH_CLOSE_CODE flip cleanly to AuthGate if needed.
  const [authState, setAuthState] = React.useState<"required" | "ok">("ok");

  const checkAuth = React.useCallback(async () => {
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

  // Hold server-pushed Active Sessions at the app level so closing or reordering
  // tiles never temporarily zeroes out Active Sessions while handles settle.
  const [serverActiveSessions, setServerActiveSessions] = React.useState<
    readonly ActiveSessionItem[]
  >([]);

  const richUiRef = React.useRef(richUi);
  richUiRef.current = richUi;
  const notificationsRef = React.useRef(notifications);
  notificationsRef.current = notifications;

  const handleGlobalServerMessage = React.useCallback((message: ServerMessage) => {
    if (message.type === "active_sessions_state") {
      setServerActiveSessions((prev) =>
        sameActiveSessions(prev, message.items) ? prev : message.items,
      );
    }
    if (message.type === "settings_state") {
      richUiRef.current.applySyncedMode(message.richUi ? "rich" : "markdown");
      void notificationsRef.current.applySyncedEnabled(message.notifications, {
        turn: message.notifyTurn,
        approval: message.notifyApproval,
        background: message.notifyBackground,
        error: message.notifyError,
      });
      return;
    }
    if (message.type === "session_state" && message.settings) {
      richUiRef.current.applySyncedMode(message.settings.richUi ? "rich" : "markdown");
      void notificationsRef.current.applySyncedEnabled(message.settings.notifications, {
        turn: message.settings.notifyTurn,
        approval: message.settings.notifyApproval,
        background: message.settings.notifyBackground,
        error: message.settings.notifyError,
      });
    }
    notificationsRef.current.notifyFromFrame(message);
  }, []);

  const sidebar = useSidebar();
  const bots = useBots();
  const [bannerHistoryBotId, setBannerHistoryBotId] = React.useState<string | null>(null);
  const [bannerEditBot, setBannerEditBot] = React.useState<BotConfig | null>(null);

  const [handles, setHandles] = React.useState<Readonly<Record<string, SessionTileHandle>>>({});
  const handlesRef = React.useRef(handles);
  handlesRef.current = handles;
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
  const handleKeyboardFocusTile = React.useCallback((tileId: string) => {
    handlesRef.current[tileId]?.focusComposer();
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
    onKeyboardFocusTile: handleKeyboardFocusTile,
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

  const rawActiveSessions =
    serverActiveSessions.length > 0
      ? serverActiveSessions
      : (primaryHandle?.activeSessions ?? serverActiveSessions);

  // Only refresh bot run history when bot-tagged active sessions change, instead
  // of re-fetching /api/bots/history on every unrelated session switch.
  const botActiveSignature = React.useMemo(
    () =>
      rawActiveSessions
        .filter((item) => item.isBot || item.title?.startsWith("[Bot]"))
        .map((item) => `${item.sessionId}:${item.kind}`)
        .join("|"),
    [rawActiveSessions],
  );
  const lastBotSigRef = React.useRef<string>("");
  React.useEffect(() => {
    if (botActiveSignature === lastBotSigRef.current) return;
    lastBotSigRef.current = botActiveSignature;
    void bots.reloadRuns();
  }, [botActiveSignature, bots.reloadRuns]);

  const mergedActiveSessions = React.useMemo<readonly ActiveSessionItem[]>(() => {
    const base = [...rawActiveSessions];
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

  const openSettings = React.useCallback(() => {
    setMcpOpen(false);
    setSettingsOpen(true);
  }, []);

  const openMcp = React.useCallback(() => {
    setSettingsOpen(false);
    setMcpOpen(true);
  }, []);

  const openWorkspacePicker = React.useCallback(() => setPicker("workspace"), []);
  const openSessionPicker = React.useCallback(() => setPicker("session"), []);
  const toggleSidebar = React.useCallback(() => setSidebarOpen((open) => !open), []);
  const consumeSharedText = React.useCallback(() => setSharedText(null), []);
  const openBotHistory = React.useCallback((botId: string) => setBannerHistoryBotId(botId), []);
  const editBannerBot = React.useCallback((bot: BotConfig) => setBannerEditBot(bot), []);
  const rerunBannerBot = React.useCallback(
    (botId: string) => {
      void bots.runNow(botId);
    },
    [bots.runNow],
  );

  const handlePaletteRun = React.useCallback(
    (name: string, arg?: string) => {
      const cleaned = name.trim().toLowerCase();
      if (cleaned === "settings" && (!arg || arg.trim() === "")) {
        openSettings();
        return;
      }
      if (cleaned === "mcp" && (!arg || arg.trim() === "")) {
        openMcp();
        return;
      }
      focusedHandle?.runCommand(name, arg);
    },
    [focusedHandle, openSettings, openMcp],
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
    onNewFolder: openWorkspacePicker,
    onRemoveFolder: sidebar.removeWorkspace,
    onOpenFolder: openSessionPicker,
    onRename: sidebar.rename,
    onHide: sidebar.hide,
    onRestore: sidebar.restore,
    onOpenArchive: openArchive,
    onOpenSettings: openSettings,
    onOpenMcp: openMcp,
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

  const focusedTileDescriptor = React.useMemo(
    () => tiling.state.tiles.find((t) => t.id === tiling.state.focusedTileId) ?? tiling.state.tiles[0],
    [tiling.state.tiles, tiling.state.focusedTileId],
  );
  const focusedTileTitle = React.useMemo(() => {
    if (!focusedTileDescriptor) return "Session";
    const sid = focusedTileDescriptor.sessionId ?? activeSession.sessionId;
    return (sid && titleBySessionId.get(sid)) || "New session";
  }, [focusedTileDescriptor, activeSession.sessionId, titleBySessionId]);

  const handleSetTileCount = React.useCallback(
    (count: 1 | 2 | 3 | 4) => {
      setSettingsOpen(false);
      setMcpOpen(false);
      tiling.setTileCount(count, activeSession.cwd || undefined);
    },
    [tiling.setTileCount, activeSession.cwd],
  );

  const handleToggleSandbox = React.useMemo(
    () =>
      focusedHandle
        ? () => focusedHandle.setSandbox(!activeSession.sandboxed)
        : undefined,
    [focusedHandle, activeSession.sandboxed],
  );

  const tileControls = React.useMemo(
    () =>
      !narrow ? (
        <TileLayoutSwitcher
          tiles={tiling.state.tiles}
          threeOrientation={tiling.state.threeOrientation}
          onSetCount={handleSetTileCount}
          onFlipThree={tiling.flipThreeOrientation}
        />
      ) : null,
    [
      narrow,
      tiling.state.tiles,
      tiling.state.threeOrientation,
      handleSetTileCount,
      tiling.flipThreeOrientation,
    ],
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
          onToggleSandbox={handleToggleSandbox}
          turnRunning={focusedHandle?.turnRunning ?? false}
          sidebarOpen={sidebarOpen}
          onToggleSidebar={toggleSidebar}
          fullWidth={showFullWidthHeader}
          tileControls={tileControls}
          watcher={activeSession.watcher}
          onConfigureWatcher={focusedHandle?.configureWatcher}
          onWatcherAction={focusedHandle?.watcherAction}
          onOpenSettings={openSettings}
        />

        {narrow && !settingsOpen && !mcpOpen ? (
          <MobileTileBar
            tiles={tiling.state.tiles}
            focusedTileId={tiling.state.focusedTileId}
            focusedTitle={focusedTileTitle}
            canAdd={tiling.canAdd}
            onFocusTile={tiling.focusTile}
            onAddTile={() => tiling.openNewTile(activeSession.cwd || undefined)}
            onCloseTile={tiling.closeTile}
          />
        ) : null}

        {settingsOpen || mcpOpen ? (
          <React.Suspense fallback={null}>
            {settingsOpen ? (
              <SettingsView
                onBack={() => setSettingsOpen(false)}
                notifications={notifications}
              />
            ) : (
              <McpView onBack={() => setMcpOpen(false)} />
            )}
          </React.Suspense>
        ) : null}

        {/* Keep TileGrid mounted while Settings/MCP overlays are open so tiles and WebSockets never reset. */}
        <div
          className={cn(
            "flex min-h-0 flex-1 flex-col",
            (settingsOpen || mcpOpen) && "hidden",
          )}
        >
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

              const resolvedTitle = tile.sessionId
                ? titleBySessionId.get(tile.sessionId)
                : undefined;
              return (
                <SessionTile
                  tileId={tile.id}
                  tileNumber={tileNumber}
                  multiTile={multiTileActive && !narrow}
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
                  sharedText={focused ? sharedText : null}
                  onSharedTextConsumed={consumeSharedText}
                  onAuthError={onAuthError}
                  onGlobalServerMessage={handleGlobalServerMessage}
                  onSessionBound={tiling.bindSessionId}
                  onHandleUpdate={onHandleUpdate}
                  onHandleUnmount={onHandleUnmount}
                  onReloadSidebar={sidebar.reload}
                  onFocus={tiling.focusTile}
                  onClose={tiling.closeTile}
                  onToggleMaximize={tiling.toggleMaximize}
                  onPromote={tiling.promoteTile}
                  onOpenSettings={openSettings}
                  onOpenMcp={openMcp}
                  onOpenPalette={palette.openPalette}
                  onOpenBotHistory={openBotHistory}
                  onEditBot={editBannerBot}
                  onRerunBot={rerunBannerBot}
                />
              );
            }}
          />
        </div>
      </div>

      <CommandPalette
        open={palette.open}
        commands={activeSession.commands}
        onOpenChange={palette.onOpenChange}
        onRun={handlePaletteRun}
      />

      <React.Suspense fallback={null}>
        {picker !== null ? (
          <FolderPicker
            open={true}
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
        ) : null}

        {bannerEditBot !== null ? (
          <CreateBotModal
            open={true}
            initialBot={bannerEditBot}
            defaultCwd={activeSession.cwd}
            onClose={() => setBannerEditBot(null)}
            onSubmit={async (payload) => {
              if (bannerEditBot) await bots.update(bannerEditBot.id, payload);
            }}
          />
        ) : null}

        {bannerHistoryBotId !== null ? (
          <BotsHistoryModal
            open={true}
            bots={bots.bots}
            initialBotId={bannerHistoryBotId}
            onClose={() => setBannerHistoryBotId(null)}
            onOpenSession={selectSession}
            onRestoreBot={bots.restore}
          />
        ) : null}

        {archiveOpen ? (
          <SessionArchiveDialog
            open={true}
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
        ) : null}
      </React.Suspense>
    </div>
  );
}
