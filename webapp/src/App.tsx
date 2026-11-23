import * as React from "react";
import { HeaderBar } from "./components/HeaderBar";
import { Transcript } from "./components/Transcript";
import { StatusLine } from "./components/StatusLine";
import { Composer } from "./components/Composer";
import { CommandPalette } from "./components/CommandPalette";
import { FolderSidebar } from "./components/FolderSidebar";
import { SidebarDrawer } from "./components/SidebarDrawer";
import { FolderPicker } from "./components/FolderPicker";
import { SessionArchiveDialog } from "./components/SessionArchiveDialog";
import { PermissionPromptCard } from "./components/PermissionPromptCard";
import { TrustFolderDialog } from "./components/TrustFolderDialog";
import { ModelPickerDialog } from "./components/ModelPickerDialog";
import { SettingsView } from "./components/SettingsView";
import { McpView } from "./components/McpView";
import { JobsTray } from "./components/JobsTray";
import { useQuartz } from "./useQuartz";
import { useCommandPalette } from "./lib/useCommandPalette";
import { useOpenUISystemPrompt, useRichUiMode } from "./lib/useRichUi";
import { useNotifications } from "./lib/useNotifications";
import { useSidebar } from "./lib/useSidebar";
import type { SessionActivity } from "./lib/sidebar";
import { useMediaQuery } from "./lib/useMediaQuery";
import { useOpenUIThemeMirror } from "./lib/theme";
import { wantsRichUi } from "./lib/richUi";
import type { ImageAttachment } from "./protocol";

/** What the folder picker is being opened for. */
type PickerMode = "session" | "workspace";

export function App(): React.ReactElement {
  // Rich UI is a preference with a consequence: it decides both how a reply is
  // drawn *and* whether the session is told to produce OpenUI Lang at all.
  const richUi = useRichUiMode();
  const systemPromptAppend = useOpenUISystemPrompt(richUi.mode);
  const notifications = useNotifications();
  const quartz = useQuartz(undefined, systemPromptAppend, notifications.notifyFromFrame);
  const sidebar = useSidebar();

  const composerRef = React.useRef<HTMLTextAreaElement | null>(null);
  const palette = useCommandPalette(composerRef);

  // OpenUI reads `data-openui-theme`; mirror whatever HeroUI applied to `<html>`
  // so a surface is never drawn in the other theme's palette.
  useOpenUIThemeMirror();

  // The sidebar is a column on a laptop and a drawer on a phone. It decides that
  // from the viewport rather than from CSS because a drawer needs a backdrop,
  // Escape handling and focus returned to its toggle -- none of which a class can do.
  const narrow = useMediaQuery("(max-width: 1023px)", false);
  const [sidebarOpen, setSidebarOpen] = React.useState(!narrow);

  React.useEffect(() => {
    // Opening on wide and closing on narrow is what a resize expects: a drawer left
    // open would cover the transcript it was meant to sit beside.
    setSidebarOpen(!narrow);
  }, [narrow]);

  const closeSidebar = React.useCallback(() => setSidebarOpen(false), []);

  const [picker, setPicker] = React.useState<PickerMode | null>(null);
  const [archiveOpen, setArchiveOpen] = React.useState(false);
  const [modelPickerOpen, setModelPickerOpen] = React.useState(false);
  const [settingsOpen, setSettingsOpen] = React.useState(false);
  const [mcpOpen, setMcpOpen] = React.useState(false);
  const [jobsOpen, setJobsOpen] = React.useState(false);
  const [dismissedTrustCwds, setDismissedTrustCwds] = React.useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const projectTrust = quartz.state.session.projectTrust;
  const trustPromptOpen = Boolean(
    projectTrust?.needsTrust && !dismissedTrustCwds.has(projectTrust.cwd),
  );

  /**
   * The control the archive dialog was opened from.
   *
   * Recorded here, at the click, rather than inside the dialog: by the time the
   * overlay is mounted its focus scope has already moved the keyboard into the
   * dialog, so `document.activeElement` there is the dialog itself. Closing gives
   * focus back to this element, which is how the keyboard stays on the archive
   * button instead of at the top of the page.
   */
  const archiveOpener = React.useRef<HTMLElement | null>(null);
  const openArchive = React.useCallback(() => {
    const active = document.activeElement;
    archiveOpener.current = active instanceof HTMLElement ? active : null;
    setArchiveOpen(true);
  }, []);

  const sessionId = quartz.state.session.sessionId;

  /**
   * Refresh the sidebar when the live session changes.
   *
   * A session started in a folder makes that folder a workspace server-side, and a
   * brand-new session has no log until its first prompt. Both facts arrive with
   * `session_state`, so this is the one place that has to re-read them.
   */
  React.useEffect(() => {
    if (sessionId) sidebar.reload();
  }, [sessionId, sidebar.reload]);

  const [sessionActivity, setSessionActivity] = React.useState<
    Readonly<Record<string, SessionActivity>>
  >({});

  // A session becomes listable as soon as the first prompt is persisted on
  // turn start, and its title/updatedAt may settle at turn end, so reload on
  // both transitions and update per-session live activity state.
  const wasRunning = React.useRef(false);
  React.useEffect(() => {
    const running = quartz.state.turnRunning;
    if (wasRunning.current !== running) sidebar.reload();

    if (sessionId) {
      if (running) {
        setSessionActivity((prev) => ({
          ...prev,
          [sessionId]: { status: "running", unread: false },
        }));
      } else if (wasRunning.current) {
        const lastRow = quartz.state.rows[quartz.state.rows.length - 1];
        const failed =
          quartz.state.fatalError !== null ||
          lastRow?.kind === "error" ||
          (lastRow?.kind === "turn_end" && lastRow.status === "error");
        const unread = typeof document !== "undefined" && document.hidden;
        setSessionActivity((prev) => ({
          ...prev,
          [sessionId]: { status: failed ? "error" : "done", unread },
        }));
      }
    }
    wasRunning.current = running;
  }, [
    quartz.state.turnRunning,
    quartz.state.fatalError,
    quartz.state.rows,
    sessionId,
    sidebar.reload,
  ]);

  React.useEffect(() => {
    const onFocus = (): void => {
      if (!sessionId) return;
      setSessionActivity((prev) => {
        const current = prev[sessionId];
        if (!current || !current.unread) return prev;
        return { ...prev, [sessionId]: { ...current, unread: false } };
      });
    };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [sessionId]);

  const onSubmit = React.useCallback(
    (text: string, images: ImageAttachment[]) => {
      quartz.prompt(text, images);
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
        setMcpOpen(false);
        setSettingsOpen(true);
        return;
      }
      if (cleaned === "mcp" && (!arg || arg.trim() === "")) {
        setSettingsOpen(false);
        setMcpOpen(true);
        return;
      }
      if (cleaned === "jobs" && (!arg || arg.trim() === "")) {
        quartz.clearBgFailed();
        setJobsOpen(true);
        return;
      }
      quartz.runCommand(name, arg);
    },
    [quartz],
  );

  // An OpenUI form submit (`@ToAssistant`) is just the next user turn: it goes
  // through the same prompt frame, so the model continues this session.
  const onPrompt = React.useCallback(
    (text: string) => {
      quartz.prompt(text, []);
    },
    [quartz],
  );

  const selectSession = React.useCallback(
    (id: string) => {
      setSettingsOpen(false);
      setMcpOpen(false);
      setSessionActivity((prev) => {
        const current = prev[id];
        if (!current || !current.unread) return prev;
        return { ...prev, [id]: { ...current, unread: false } };
      });
      quartz.loadSession(id);
      if (narrow) closeSidebar();
    },
    [quartz, narrow, closeSidebar],
  );

  const newSessionIn = React.useCallback(
    (cwd: string) => {
      setSettingsOpen(false);
      setMcpOpen(false);
      quartz.newSession(cwd);
      if (narrow) closeSidebar();
    },
    [quartz, narrow, closeSidebar],
  );

  const sidebarProps = {
    data: sidebar.data,
    loading: sidebar.loading,
    error: sidebar.error,
    actionError: sidebar.actionError,
    currentSessionId: sessionId,
    currentCwd: quartz.state.session.cwd,
    onClose: closeSidebar,
    onReload: sidebar.reload,
    onSelectSession: selectSession,
    onNewSession: newSessionIn,
    onNewFolder: () => setPicker("workspace"),
    onOpenFolder: () => setPicker("session"),
    // Handed over as-is: each one resolves to whether the server did it, which is
    // what the sidebar's undo notice reports on.
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
    sessionActivity,
    inbox: quartz.state.inbox ?? [],
  };

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
          session={quartz.state.session}
          status={quartz.status}
          richUiMode={richUi.mode}
          onRichUiModeChange={richUi.setMode}
          onNewSession={() => {
            if (quartz.state.session.cwd) newSessionIn(quartz.state.session.cwd);
            else setPicker("session");
          }}
          onChangeFolder={() => setPicker("session")}
          sidebarOpen={sidebarOpen}
          onToggleSidebar={() => setSidebarOpen((open) => !open)}
        />
        {settingsOpen ? (
          <SettingsView
            onBack={() => {
              setSettingsOpen(false);
            }}
            notifications={notifications}
          />
        ) : mcpOpen ? (
          <McpView
            onBack={() => {
              setMcpOpen(false);
            }}
          />
        ) : (
          <>
            <Transcript
              rows={quartz.state.rows}
              turnRunning={quartz.state.turnRunning}
              cwd={quartz.state.session.cwd}
              richUi={wantsRichUi(richUi.mode)}
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
              onClose={() => {
                setJobsOpen(false);
              }}
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
              onOpenPalette={palette.openPalette}
              textareaRef={composerRef}
            />
          </>
        )}
      </div>

      <CommandPalette
        open={palette.open}
        commands={quartz.state.session.commands}
        onOpenChange={palette.onOpenChange}
        onRun={handleRunCommand}
      />

      <FolderPicker
        open={picker !== null}
        onOpenChange={(open) => {
          if (!open) setPicker(null);
        }}
        title={picker === "workspace" ? "Add a folder" : "New session"}
        chooseLabel={picker === "workspace" ? "Add to sidebar" : "Start session"}
        onChoose={(path) => {
          // Both paths are real requests: the picker never invents a session, and
          // adding a folder is the server's choice to remember.
          if (picker === "workspace") void sidebar.addWorkspace(path);
          else newSessionIn(path);
        }}
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
    </div>
  );
}
