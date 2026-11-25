import * as React from "react";
import { Button, Spinner, cn } from "@heroui/react";
import { RefreshCw } from "lucide-react";
import { ActionNotice, type Notice, type NoticeAction } from "./sidebar/ActionNotice";
import { SidebarFooter } from "./sidebar/SidebarFooter";
import { ActiveSessionsSection } from "./sidebar/ActiveSessionsSection";
import { BotsHistoryModal } from "./BotsHistoryModal";
import { BotsSection } from "./BotsSection";
import { CreateBotModal } from "./CreateBotModal";
import { EmptySidebar } from "./sidebar/EmptySidebar";
import { SessionRow } from "./sidebar/SessionRow";
import { SidebarHeader } from "./sidebar/SidebarHeader";
import { WorkspaceRow } from "./sidebar/WorkspaceRow";
import {
  currentWorkspaceCwd,
  displayTitle,
  EMPTY_SIDEBAR,
  filterWorkspaceGroups,
  folderBadge,
  groupSessions,
  NOTICE_TIMEOUT_MS,
  readSidebarCollapseState,
  writeSidebarCollapseState,
  type LiveSession,
  type SessionActivity,
  type SidebarCollapseState,
  type SidebarPayload,
  type SidebarSession,
  type SidebarState,
} from "@/lib/sidebar";
import type { ActiveSessionItem } from "@/protocol";
import type { BotConfig, BotFormValues } from "@/types/bots";

export interface FolderSidebarProps {
  /** The last good payload, or null before the first load succeeds. */
  data: SidebarPayload | null;
  loading: boolean;
  /** A fetch failure. Shown instead of the list, with a retry. */
  error: string | null;
  /** A failed action (hide, rename). Shown above the list; the data is unchanged. */
  actionError: string | null;
  currentSessionId: string;
  /**
   * The live session's folder.
   *
   * A fresh session has no messages, so it has no log and no row: this is the only
   * thing on screen that can acknowledge the `+` that just created it.
   */
  currentCwd: string;
  /** `rail` is the desktop column; `drawer` is the phone overlay. */
  variant: "rail" | "drawer";
  onClose: () => void;
  onReload: () => void;
  onSelectSession: (sessionId: string) => void;
  onNewSession: (cwd: string) => void;
  /** Adds a folder to the sidebar; the header's folder button. */
  onNewFolder: () => void;
  /** Removes an empty folder from the sidebar list (never deletes on disk). */
  onRemoveFolder?: (cwd: string) => Promise<boolean>;
  /** Opens the folder picker to start a session in a folder picked there. */
  onOpenFolder: () => void;
  /** Both resolve to whether the server did it, which is what the notice reports. */
  onRename: (sessionId: string, title: string) => Promise<boolean>;
  onHide: (sessionId: string) => Promise<boolean>;
  /** The undo for a hide; the same request the archive's Restore makes. */
  onRestore: (sessionId: string) => Promise<boolean>;
  onOpenArchive: () => void;
  /** Opens the in-canvas settings view. */
  onOpenSettings?: () => void;
  /** Opens the in-canvas MCP servers view. */
  onOpenMcp?: () => void;
  /** Per-session live status (running / done / error / unread). */
  sessionActivity?: Readonly<Record<string, SessionActivity>>;
  /** Sessions needing attention (permission prompt, finished turn, or error). */
  activeSessions?: readonly ActiveSessionItem[];
  /** Dismiss a non-permission item from Active Sessions. */
  onDismissActiveSession?: (sessionId: string) => void;
  /** Clear all read non-permission items from Active Sessions. */
  onClearReadActiveSessions?: () => void;
  /** Configured autonomous bots. */
  bots?: readonly BotConfig[];
  togglingBotIds?: ReadonlySet<string>;
  runningBotIds?: ReadonlySet<string>;
  botToggleError?: string | null;
  onCreateBot?: (payload: BotFormValues) => void | Promise<void>;
  onUpdateBot?: (id: string, payload: BotFormValues) => void | Promise<void>;
  onToggleBot?: (id: string, enabled: boolean) => void | Promise<void>;
  onRunBot?: (id: string) => void | Promise<void>;
  onDeleteBot?: (id: string) => void | Promise<void>;
  onRestoreBot?: (id: string) => Promise<unknown>;
}

const EMPTY_STATE: SidebarState = EMPTY_SIDEBAR;

/**
 * The folder-first session sidebar.
 *
 * Two levels, like the reference: a workspace (a `cwd`) and the sessions started in
 * it. It is the only place a person needs the terminal's `/session` command, so
 * every action here is a real request -- nothing is applied locally and then hoped
 * for.
 *
 * The state it draws is server-side (`~/.quartz/web/sidebar.json`), which is why a
 * reload shows exactly what was on screen: hides, renames and imported folders are
 * not this component's state at all.
 */
export function FolderSidebar({
  data,
  loading,
  error,
  actionError,
  currentSessionId,
  currentCwd,
  variant,
  onClose,
  onReload,
  onSelectSession,
  onNewSession,
  onNewFolder,
  onRemoveFolder,
  onOpenFolder,
  onRename,
  onHide,
  onRestore,
  onOpenArchive,
  onOpenSettings,
  onOpenMcp,
  sessionActivity = {},
  activeSessions: activeSessionItems = [],
  onDismissActiveSession,
  bots = [],
  togglingBotIds,
  runningBotIds,
  botToggleError,
  onCreateBot,
  onUpdateBot,
  onToggleBot,
  onRunBot,
  onDeleteBot,
  onRestoreBot,
}: FolderSidebarProps): React.ReactElement {
  const [collapseState, setCollapseState] = React.useState<SidebarCollapseState>(() =>
    readSidebarCollapseState(),
  );
  const expandedOverrides = collapseState.folders;
  const [searchQuery, setSearchQuery] = React.useState("");

  const groups = React.useMemo(() => (data ? groupSessions(data) : []), [data]);
  const sidebarState = data?.sidebar ?? EMPTY_STATE;
  const visibleGroups = React.useMemo(
    () => filterWorkspaceGroups(groups, searchQuery, sidebarState),
    [groups, searchQuery, sidebarState],
  );
  const searching = searchQuery.trim() !== "";
  const filtering = searching;
  const sessions = React.useMemo(() => data?.sessions ?? [], [data]);
  const sessionById = React.useMemo(() => {
    const map = new Map<string, SidebarSession>();
    for (const s of sessions) map.set(s.sessionId, s);
    return map;
  }, [sessions]);
  const activeSessions = React.useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    const filtered = q
      ? activeSessionItems.filter((item) => {
          const s = sessionById.get(item.sessionId);
          const title = sidebarState.renames[item.sessionId] ?? s?.title ?? item.title ?? "";
          const preview = item.preview ?? "";
          return title.toLowerCase().includes(q) || preview.toLowerCase().includes(q);
        })
      : [...activeSessionItems];
    const priority = (kind: ActiveSessionItem["kind"]): number => {
      if (kind === "permission" || kind === "error") return 0;
      if (kind === "working" || kind === "waiting") return 1;
      return 2;
    };
    return [...filtered].sort((a, b) => priority(a.kind) - priority(b.kind));
  }, [activeSessionItems, searchQuery, sessionById, sidebarState.renames]);
  const drawer = variant === "drawer";

  const live = React.useMemo<LiveSession>(
    () => ({ sessionId: currentSessionId, cwd: currentCwd }),
    [currentSessionId, currentCwd],
  );
  // One folder at most is the one the next prompt lands in, decided in `lib/` so
  // the rule is testable without a browser. Everything below compares with `===`
  // against this value, which is what stops two folders looking current.
  const current = React.useMemo(() => currentWorkspaceCwd(groups, live), [groups, live]);

  const [notice, setNotice] = React.useState<Notice | null>(null);
  const [undoing, setUndoing] = React.useState(false);
  const [noticeFocused, setNoticeFocused] = React.useState(false);
  const [botModalOpen, setBotModalOpen] = React.useState(false);
  const [editingBot, setEditingBot] = React.useState<BotConfig | null>(null);
  const [historyModalOpen, setHistoryModalOpen] = React.useState(false);

  // A notice dismisses itself, and every new one restarts the clock: the person
  // acting twice gets the second confirmation, not a countdown inherited from the
  // first. It is held while the keyboard is inside it, so Undo can never expire
  // mid-reach.
  React.useEffect(() => {
    if (notice === null || noticeFocused) return;
    const timer = window.setTimeout(() => setNotice(null), NOTICE_TIMEOUT_MS);
    return () => window.clearTimeout(timer);
  }, [notice, noticeFocused]);

  const hide = React.useCallback(
    async (sessionId: string, title: string): Promise<void> => {
      const hidden = await onHide(sessionId);
      // A hide that failed is already announced above the list, with the server's
      // own message; a second copy here would be noise rather than a confirmation.
      if (!hidden) return;
      setNotice({ action: { kind: "hide", sessionId, title } });
    },
    [onHide],
  );

  const rename = React.useCallback(
    async (sessionId: string, title: string, previousTitle: string): Promise<void> => {
      const renamed = await onRename(sessionId, title);
      if (!renamed) return;
      setNotice({ action: { kind: "rename", sessionId, title, previousTitle } });
    },
    [onRename],
  );

  const undo = React.useCallback(
    async (action: NoticeAction): Promise<void> => {
      setUndoing(true);
      try {
        const done =
          action.kind === "hide"
            ? await onRestore(action.sessionId)
            // An empty title clears the rename server-side, which is exactly how
            // the title went back to its preview before the rename happened.
            : await onRename(action.sessionId, action.previousTitle);
        if (done) {
          setNotice(null);
          return;
        }
        // Never pretend it worked: the notice stays, says what failed, and offers
        // the same request again.
        setNotice({
          action,
          error:
            action.kind === "hide"
              ? `Could not restore “${action.title}”.`
              : "Could not undo the rename.",
        });
      } finally {
        setUndoing(false);
      }
    },
    [onRestore, onRename],
  );

  const updateCollapseState = React.useCallback(
    (updater: (prev: SidebarCollapseState) => SidebarCollapseState) => {
      setCollapseState((prev) => {
        const next = updater(prev);
        writeSidebarCollapseState(next);
        return next;
      });
    },
    [],
  );

  const toggle = React.useCallback(
    (cwd: string, expanded: boolean) => {
      updateCollapseState((prev) => ({
        ...prev,
        folders: { ...prev.folders, [cwd]: expanded },
      }));
    },
    [updateCollapseState],
  );

  return (
    <nav
      aria-label="Sessions"
      data-variant={variant}
      className={cn(
        "flex h-full min-h-0 flex-col bg-background",
        // The drawer's size and position belong to `SidebarDrawer`, which also owns
        // Escape and focus: a rail is a column, a drawer is a panel over the page.
        drawer ? "w-full border-r border-separator" : "w-64 shrink-0 border-r border-separator",
      )}
    >
      <SidebarHeader
        drawer={drawer}
        searchQuery={searchQuery}
        onNewFolder={onNewFolder}
        onClose={onClose}
        onSearchChange={setSearchQuery}
      />

      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-1.5">
        {error !== null ? (
          <div role="alert" className="rounded-xl border border-danger/40 bg-danger-soft p-3 text-sm">
            <p className="font-medium text-foreground">Could not load sessions</p>
            <p className="mt-1 break-words text-xs text-muted">{error}</p>
            <Button size="sm" variant="outline" className="mt-2" onPress={onReload}>
              <RefreshCw aria-hidden="true" className="size-3.5" />
              Retry
            </Button>
          </div>
        ) : null}

        {error === null && loading && data === null ? (
          <p role="status" className="flex items-center gap-2 px-1 py-3 text-sm text-muted">
            <Spinner size="sm" aria-label="Loading sessions" />
            Loading sessions…
          </p>
        ) : null}

        {actionError !== null ? (
          <p
            role="alert"
            className="mb-2 rounded-lg bg-danger-soft px-2 py-1.5 text-xs text-danger-soft-foreground"
          >
            {actionError}
          </p>
        ) : null}

        {error === null ? (
          <>
            <ActiveSessionsSection
              items={activeSessions}
              currentSessionId={currentSessionId}
              sessionById={sessionById}
              renames={sidebarState.renames}
              onSelectSession={onSelectSession}
              onDismiss={onDismissActiveSession}
            />
            <BotsSection
              bots={bots}
              togglingIds={togglingBotIds}
              runningIds={runningBotIds}
              toggleError={botToggleError}
              onCreateBot={() => {
                setEditingBot(null);
                setBotModalOpen(true);
              }}
              onOpenHistory={() => setHistoryModalOpen(true)}
              onEditBot={(bot) => {
                setEditingBot(bot);
                setBotModalOpen(true);
              }}
              onToggleBot={(id, enabled) => {
                void onToggleBot?.(id, enabled);
              }}
              onRunBot={
                onRunBot
                  ? (id) => {
                      void onRunBot(id);
                    }
                  : undefined
              }
              onDeleteBot={(id) => {
                void onDeleteBot?.(id);
              }}
            />
          </>
        ) : null}

        {error === null && data !== null
          ? groups.length === 0
            ? (
              <EmptySidebar onOpenFolder={onOpenFolder} onOpenArchive={onOpenArchive} />
            )
            : visibleGroups.length === 0
              ? (
                <div className="px-2 py-4 text-center text-xs text-muted">
                  <p>No matches for “{searchQuery.trim()}”.</p>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="mt-1.5 h-6 text-xs"
                    onPress={() => setSearchQuery("")}
                  >
                    Clear filter
                  </Button>
                </div>
              )
              : (
              <ul className="space-y-0.5">
                {visibleGroups.map((group) => {
                  const here = group.cwd === current;
                  return (
                    <li key={group.cwd}>
                      <WorkspaceRow
                        group={group}
                        current={here}
                        badge={folderBadge(group, live, sessions)}
                        expanded={
                          filtering || (expandedOverrides[group.cwd] ?? (group.sessions.length > 0 || here))
                        }
                        onToggle={(expanded) => toggle(group.cwd, expanded)}
                        onNewSession={() => onNewSession(group.cwd)}
                        onRemove={
                          onRemoveFolder && group.sessions.length === 0
                            ? () => void onRemoveFolder(group.cwd)
                            : undefined
                        }
                      >
                        {group.sessions.map((session) => (
                          <SessionRow
                            key={session.sessionId}
                            session={session}
                            sidebar={sidebarState}
                            activity={sessionActivity[session.sessionId]}
                            // Both halves: a row is current only while its own
                            // folder is the live one, so a row can never claim to
                            // be where the next prompt lands when its folder is not.
                            current={
                              here &&
                              currentSessionId !== "" &&
                              session.sessionId === currentSessionId
                            }
                            onSelect={() => onSelectSession(session.sessionId)}
                            onRename={(title) =>
                              void rename(
                                session.sessionId,
                                title,
                                sidebarState.renames[session.sessionId] ?? "",
                              )
                            }
                            onHide={() =>
                              void hide(session.sessionId, displayTitle(session, sidebarState))
                            }
                          />
                        ))}
                      </WorkspaceRow>
                    </li>
                  );
                })}
              </ul>
            )
          : null}
      </div>

      {/* A live region that is always mounted, because one that appears together
          with its text is not reliably announced. It sits below the scrolling list
          and outside it: the confirmation belongs where the action happened, it
          pushes nothing aside when it arrives, and it cannot cover the composer or
          the last message, which live in the other column. */}
      <div
        role="status"
        aria-live="polite"
        aria-atomic="true"
        onFocus={() => setNoticeFocused(true)}
        onBlur={() => setNoticeFocused(false)}
        className={cn(
          "shrink-0",
          notice === null ? "" : "border-t border-separator px-2 py-2",
        )}
      >
        {notice === null ? null : (
          <ActionNotice
            notice={notice}
            busy={undoing}
            onUndo={() => void undo(notice.action)}
            onDismiss={() => setNotice(null)}
          />
        )}
      </div>

      <SidebarFooter
        drawer={drawer}
        onClose={onClose}
        onOpenArchive={onOpenArchive}
        onOpenMcp={onOpenMcp}
        onOpenSettings={onOpenSettings}
      />

      <CreateBotModal
        open={botModalOpen}
        initialBot={editingBot}
        defaultCwd={currentCwd}
        onClose={() => {
          setBotModalOpen(false);
          setEditingBot(null);
        }}
        onSubmit={async (payload) => {
          if (editingBot) {
            await onUpdateBot?.(editingBot.id, payload);
          } else {
            await onCreateBot?.(payload);
          }
        }}
      />

      <BotsHistoryModal
        open={historyModalOpen}
        bots={bots}
        onClose={() => setHistoryModalOpen(false)}
        onOpenSession={onSelectSession}
        onRestoreBot={onRestoreBot}
      />
    </nav>
  );
}
