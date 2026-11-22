import * as React from "react";
import { Button, Chip, Disclosure, Spinner, cn } from "@heroui/react";
import {
  ArchiveRestore,
  ChevronRight,
  FolderOpen,
  FolderPlus,
  Pencil,
  Plus,
  RefreshCw,
  Undo2,
  X,
} from "lucide-react";
import {
  currentWorkspaceCwd,
  displayTitle,
  folderBadge,
  formatAge,
  groupSessions,
  NOTICE_TIMEOUT_MS,
  type FolderBadge,
  type LiveSession,
  type SidebarPayload,
  type SidebarSession,
  type SidebarState,
  type WorkspaceGroup,
} from "@/lib/sidebar";

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
  /** Opens the folder picker to start a session in a folder picked there. */
  onOpenFolder: () => void;
  /** Both resolve to whether the server did it, which is what the notice reports. */
  onRename: (sessionId: string, title: string) => Promise<boolean>;
  onHide: (sessionId: string) => Promise<boolean>;
  /** The undo for a hide; the same request the archive's Restore makes. */
  onRestore: (sessionId: string) => Promise<boolean>;
  onOpenArchive: () => void;
}

const EMPTY_STATE: SidebarState = {
  version: 1,
  hiddenSessions: [],
  importedSessions: [],
  renames: {},
  workspaces: [],
};

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
  onOpenFolder,
  onRename,
  onHide,
  onRestore,
  onOpenArchive,
}: FolderSidebarProps): React.ReactElement {
  const [collapsed, setCollapsed] = React.useState<ReadonlySet<string>>(new Set());

  const groups = React.useMemo(() => (data ? groupSessions(data) : []), [data]);
  const sidebarState = data?.sidebar ?? EMPTY_STATE;
  const sessions = React.useMemo(() => data?.sessions ?? [], [data]);
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

  const toggle = React.useCallback((cwd: string, expanded: boolean) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (expanded) next.delete(cwd);
      else next.add(cwd);
      return next;
    });
  }, []);

  return (
    <nav
      aria-label="Sessions"
      data-variant={variant}
      className={cn(
        "flex h-full min-h-0 flex-col bg-background",
        // The drawer's size and position belong to `SidebarDrawer`, which also owns
        // Escape and focus: a rail is a column, a drawer is a panel over the page.
        drawer ? "w-full border-r border-separator" : "w-72 shrink-0 border-r border-separator",
      )}
    >
      <div className="flex shrink-0 items-center gap-1 border-b border-separator px-2 py-2">
        <h2 className="min-w-0 flex-1 truncate px-1 text-xs font-semibold uppercase tracking-wider text-muted">
          Workspaces
        </h2>
        <Button
          isIconOnly
          size="sm"
          variant="ghost"
          aria-label="Add a folder to the sidebar"
          onPress={onNewFolder}
        >
          <FolderPlus aria-hidden="true" className="size-4" />
        </Button>
        <Button isIconOnly size="sm" variant="ghost" aria-label="Refresh sessions" onPress={onReload}>
          <RefreshCw aria-hidden="true" className="size-4" />
        </Button>
        <Button
          isIconOnly
          size="sm"
          variant="ghost"
          aria-label="Open the session archive"
          onPress={onOpenArchive}
        >
          <ArchiveRestore aria-hidden="true" className="size-4" />
        </Button>
        {drawer ? (
          <Button
            isIconOnly
            size="sm"
            variant="ghost"
            aria-label="Close the session sidebar"
            onPress={onClose}
          >
            <X aria-hidden="true" className="size-4" />
          </Button>
        ) : null}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-2 py-2">
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

        {error === null && data !== null
          ? groups.length === 0
            ? (
              <EmptySidebar onOpenFolder={onOpenFolder} onOpenArchive={onOpenArchive} />
            )
            : (
              <ul className="space-y-1">
                {groups.map((group) => {
                  const here = group.cwd === current;
                  return (
                    <li key={group.cwd}>
                      <WorkspaceRow
                        group={group}
                        current={here}
                        badge={folderBadge(group, live, sessions)}
                        expanded={!collapsed.has(group.cwd)}
                        onToggle={(expanded) => toggle(group.cwd, expanded)}
                        onNewSession={() => onNewSession(group.cwd)}
                      >
                        {group.sessions.map((session, index) => (
                          <SessionRow
                            key={session.sessionId}
                            session={session}
                            sidebar={sidebarState}
                            // Both halves: a row is current only while its own
                            // folder is the live one, so a row can never claim to
                            // be where the next prompt lands when its folder is not.
                            current={
                              here &&
                              currentSessionId !== "" &&
                              session.sessionId === currentSessionId
                            }
                            last={index === group.sessions.length - 1}
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
    </nav>
  );
}

/**
 * What the sidebar is confirming, and what undoing it would take.
 *
 * The action is kept whole rather than as a callback so the notice can offer the
 * same request twice: an undo that fails changes the sentence, not the affordance.
 */
type NoticeAction =
  | { kind: "hide"; sessionId: string; title: string }
  | { kind: "rename"; sessionId: string; title: string; previousTitle: string };

interface Notice {
  action: NoticeAction;
  /** Set when the undo itself failed. The notice stays on screen and says so. */
  error?: string;
}

function noticeMessage(action: NoticeAction): string {
  if (action.kind === "hide") return `Hidden “${action.title}”.`;
  return action.title === "" ? "Rename cleared." : `Renamed to “${action.title}”.`;
}

/**
 * An undoable confirmation: one sentence, one action, one way out.
 *
 * It is part of the sidebar rather than a floating toast because that is where the
 * change happened and because the sidebar column cannot cover the composer. It
 * takes no focus: a confirmation that moves the caret out of a half-written prompt
 * is worse than the row it replaced. Every control in it is a normal tab stop, so
 * the keyboard reaches it without a hover.
 */
function ActionNotice({
  notice,
  busy,
  onUndo,
  onDismiss,
}: {
  notice: Notice;
  busy: boolean;
  onUndo: () => void;
  onDismiss: () => void;
}): React.ReactElement {
  const failed = notice.error !== undefined;

  return (
    <div
      className={cn(
        "flex items-center gap-1.5 rounded-lg border px-2 py-1.5",
        failed ? "border-danger/40 bg-danger-soft" : "border-border bg-surface",
      )}
    >
      <p
        className={cn(
          "min-w-0 flex-1 text-[11px] leading-snug break-words",
          failed ? "text-danger-soft-foreground" : "text-foreground",
        )}
      >
        {notice.error ?? noticeMessage(notice.action)}
      </p>
      <Button
        size="sm"
        variant={failed ? "outline" : "ghost"}
        isDisabled={busy}
        onPress={onUndo}
        className="shrink-0 text-[11px]"
      >
        <Undo2 aria-hidden="true" className="size-3" />
        {failed ? "Try again" : "Undo"}
      </Button>
      <Button
        isIconOnly
        size="sm"
        variant="ghost"
        aria-label="Dismiss notification"
        onPress={onDismiss}
        className="shrink-0"
      >
        <X aria-hidden="true" className="size-3" />
      </Button>
    </div>
  );
}

/**
 * The whole-sidebar empty state.
 *
 * A first run has nothing to show and that is correct: every session that exists
 * was started in a terminal, and the sidebar lists the browser's own. Saying only
 * that would leave a person staring at an empty column, so the two ways to fill it
 * are right here -- open a folder and start something, or bring in what the
 * terminal already has.
 */
function EmptySidebar({
  onOpenFolder,
  onOpenArchive,
}: {
  onOpenFolder: () => void;
  onOpenArchive: () => void;
}): React.ReactElement {
  return (
    <div className="rounded-xl border border-dashed border-border p-3 text-center">
      <p className="text-sm font-medium text-foreground">No sessions yet</p>
      <p className="mt-1 text-xs text-muted">
        Open a folder to start one, or import a session you began in the terminal.
      </p>
      <div className="mt-3 flex flex-col gap-1.5">
        <Button size="sm" variant="outline" onPress={onOpenFolder}>
          <FolderOpen aria-hidden="true" className="size-3.5" />
          Open a folder
        </Button>
        <Button size="sm" variant="outline" onPress={onOpenArchive}>
          <ArchiveRestore aria-hidden="true" className="size-3.5" />
          Import from terminal
        </Button>
      </div>
    </div>
  );
}

function WorkspaceRow({
  group,
  current,
  badge,
  expanded,
  onToggle,
  onNewSession,
  children,
}: {
  group: WorkspaceGroup;
  /** The live session runs here, so this is where the next prompt lands. */
  current: boolean;
  /** `new` only while this folder is waiting for its first session row. */
  badge: FolderBadge | null;
  expanded: boolean;
  onToggle: (expanded: boolean) => void;
  onNewSession: () => void;
  children: React.ReactNode;
}): React.ReactElement {
  const count = group.sessions.length;

  return (
    <Disclosure
      isExpanded={expanded}
      onExpandedChange={onToggle}
      className={cn("rounded-lg", current ? "bg-accent-soft" : "hover:bg-surface-secondary")}
    >
      <div className="group/workspace flex items-center gap-0.5 pl-0.5 pr-1">
        <Disclosure.Heading className="min-w-0 flex-1">
          <Disclosure.Trigger
            aria-label={`${group.label} folder`}
            aria-current={current ? "true" : undefined}
            className="flex min-h-9 w-full cursor-pointer items-center gap-1 rounded-md px-1 py-1 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
          >
            <ChevronRight
              aria-hidden="true"
              className={cn(
                "size-3.5 shrink-0 text-muted motion-safe:transition-transform",
                expanded && "rotate-90",
              )}
            />
            <span
              className={cn(
                "min-w-0 truncate text-sm",
                current && "font-medium text-accent-soft-foreground",
              )}
              title={group.cwd}
            >
              {group.label}
            </span>
            {count > 0 ? (
              <Chip size="sm" variant="soft" color="default" className="ml-auto shrink-0">
                {count}
              </Chip>
            ) : badge === null ? null : (
              // 11px and a token with room to spare: this used to be 10px at
              // 4.4:1, which is under the floor for text this small.
              <span className="ml-auto shrink-0 text-[11px] uppercase tracking-wide text-meta">
                {badge}
              </span>
            )}
          </Disclosure.Trigger>
        </Disclosure.Heading>
        <Button
          isIconOnly
          size="sm"
          variant="ghost"
          aria-label={`Start a new session in ${group.label}`}
          onPress={onNewSession}
          // Visible by default: a phone has no hover, so a hover-only "+" is a
          // feature nobody on a touch screen can reach.
          className="shrink-0 motion-safe:transition-opacity [@media(hover:hover)]:opacity-0 group-hover/workspace:opacity-100 group-focus-within/workspace:opacity-100"
        >
          <Plus aria-hidden="true" className="size-3.5" />
        </Button>
      </div>
      <Disclosure.Content>
        <Disclosure.Body className="ml-3">
          {count === 0 ? (
            <p className="py-1 pl-4 text-xs text-muted">No sessions in this folder yet.</p>
          ) : (
            <ul>{children}</ul>
          )}
        </Disclosure.Body>
      </Disclosure.Content>
    </Disclosure>
  );
}

function SessionRow({
  session,
  sidebar,
  current,
  last,
  onSelect,
  onRename,
  onHide,
}: {
  session: SidebarSession;
  sidebar: SidebarState;
  current: boolean;
  last: boolean;
  onSelect: () => void;
  onRename: (title: string) => void;
  onHide: () => void;
}): React.ReactElement {
  const [renaming, setRenaming] = React.useState(false);
  const [draft, setDraft] = React.useState("");
  const inputId = React.useId();
  const inputRef = React.useRef<HTMLInputElement | null>(null);
  const title = displayTitle(session, sidebar);
  const age = formatAge(session.updatedAt);

  React.useEffect(() => {
    if (!renaming) return;
    inputRef.current?.focus();
    inputRef.current?.select();
  }, [renaming]);

  const startRename = (): void => {
    setDraft(title);
    setRenaming(true);
  };

  const commit = (): void => {
    setRenaming(false);
    const next = draft.trim();
    // An empty draft clears the rename server-side, which is the only way back to
    // the preview title without a terminal.
    if (next !== title) onRename(next);
  };

  return (
    <li className="relative pl-4">
      {/* Tree connectors, so a session reads as belonging to the folder above it. */}
      {!last ? (
        <span
          aria-hidden="true"
          className="pointer-events-none absolute top-0 bottom-0 left-0 w-px bg-border"
        />
      ) : null}
      <span
        aria-hidden="true"
        className="pointer-events-none absolute top-0 left-0 h-4 w-3 rounded-bl-[4px] border-b border-l border-border"
      />

      {renaming ? (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            commit();
          }}
          className="flex items-center gap-1 rounded-md bg-surface-secondary px-1.5 py-1"
        >
          <label htmlFor={inputId} className="sr-only">
            Session title
          </label>
          <input
            id={inputId}
            ref={inputRef}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.stopPropagation();
                setRenaming(false);
              }
            }}
            onBlur={commit}
            className="min-w-0 flex-1 rounded border border-border bg-background px-1.5 py-0.5 text-xs text-foreground"
          />
        </form>
      ) : (
        <div
          className={cn(
            "group/session relative flex min-h-9 items-center gap-1 rounded-md pr-1 transition-colors",
            current
              ? "bg-accent-soft font-medium text-accent-soft-foreground"
              : "hover:bg-surface-secondary",
          )}
        >
          <button
            type="button"
            onClick={onSelect}
            aria-current={current ? "true" : undefined}
            className="flex min-h-9 min-w-0 flex-1 cursor-pointer items-center rounded-md px-1.5 text-left text-[13px] focus-visible:ring-2 focus-visible:ring-focus focus-visible:outline-none"
          >
            <span className="truncate" title={`${title}${age ? ` · ${age} old` : ""}`}>
              {title}
            </span>
          </button>

          {age ? (
            <span
              aria-hidden="true"
              // 11px at `--meta` (>= 5:1 in both themes) instead of 10px at
              // `--muted`'s 4.4:1, which is where "6h" stopped being readable.
              className="shrink-0 text-[11px] text-meta tabular-nums group-hover/session:hidden group-focus-within/session:hidden"
            >
              {age}
            </span>
          ) : null}

          {/* Hover on a mouse, `focus-within` for a keyboard: the row's actions are
              always the next tab stop, never mouse-only. The current row shows them
              without being asked -- on a desktop nobody hovers a row to discover
              that Rename exists, and this is the one row you are already working in,
              so it is what makes the affordance discoverable everywhere else. */}
          <div
            className={cn(
              "flex shrink-0 items-center motion-safe:transition-opacity",
              current
                ? "opacity-100"
                : "[@media(hover:hover)]:opacity-0 group-hover/session:opacity-100 group-focus-within/session:opacity-100",
            )}
          >
            <Button
              isIconOnly
              size="sm"
              variant="ghost"
              aria-label={`Rename session ${title}`}
              onPress={startRename}
            >
              <Pencil aria-hidden="true" className="size-3" />
            </Button>
            <Button
              isIconOnly
              size="sm"
              variant="ghost"
              aria-label={`Hide session ${title} from the sidebar`}
              onPress={onHide}
            >
              <X aria-hidden="true" className="size-3" />
            </Button>
          </div>
        </div>
      )}
    </li>
  );
}
