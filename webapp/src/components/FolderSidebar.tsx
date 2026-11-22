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
  X,
} from "lucide-react";
import {
  displayTitle,
  formatAge,
  groupSessions,
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
  onRename: (sessionId: string, title: string) => void;
  onHide: (sessionId: string) => void;
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
  variant,
  onClose,
  onReload,
  onSelectSession,
  onNewSession,
  onNewFolder,
  onOpenFolder,
  onRename,
  onHide,
  onOpenArchive,
}: FolderSidebarProps): React.ReactElement {
  const [collapsed, setCollapsed] = React.useState<ReadonlySet<string>>(new Set());

  const groups = React.useMemo(() => (data ? groupSessions(data) : []), [data]);
  const sidebarState = data?.sidebar ?? EMPTY_STATE;
  const drawer = variant === "drawer";

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
                {groups.map((group) => (
                  <li key={group.cwd}>
                    <WorkspaceRow
                      group={group}
                      expanded={!collapsed.has(group.cwd)}
                      onToggle={(expanded) => toggle(group.cwd, expanded)}
                      onNewSession={() => onNewSession(group.cwd)}
                    >
                      {group.sessions.map((session, index) => (
                        <SessionRow
                          key={session.sessionId}
                          session={session}
                          sidebar={sidebarState}
                          current={session.sessionId === currentSessionId}
                          last={index === group.sessions.length - 1}
                          onSelect={() => onSelectSession(session.sessionId)}
                          onRename={(title) => onRename(session.sessionId, title)}
                          onHide={() => onHide(session.sessionId)}
                        />
                      ))}
                    </WorkspaceRow>
                  </li>
                ))}
              </ul>
            )
          : null}
      </div>
    </nav>
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
  expanded,
  onToggle,
  onNewSession,
  children,
}: {
  group: WorkspaceGroup;
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
      className="rounded-lg hover:bg-surface-secondary"
    >
      <div className="group/workspace flex items-center gap-0.5 pl-0.5 pr-1">
        <Disclosure.Heading className="min-w-0 flex-1">
          <Disclosure.Trigger
            aria-label={`${group.label} folder`}
            className="flex min-h-9 w-full cursor-pointer items-center gap-1 rounded-md px-1 py-1 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
          >
            <ChevronRight
              aria-hidden="true"
              className={cn(
                "size-3.5 shrink-0 text-muted motion-safe:transition-transform",
                expanded && "rotate-90",
              )}
            />
            <span className="min-w-0 truncate text-sm" title={group.cwd}>
              {group.label}
            </span>
            {count > 0 ? (
              <Chip size="sm" variant="soft" color="default" className="ml-auto shrink-0">
                {count}
              </Chip>
            ) : (
              <span className="ml-auto shrink-0 text-[10px] uppercase tracking-wide text-muted">
                empty
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
              className="shrink-0 text-[10px] text-muted tabular-nums group-hover/session:hidden group-focus-within/session:hidden"
            >
              {age}
            </span>
          ) : null}

          {/* Hover on a mouse, `focus-within` for a keyboard: the row's actions are
              always the next tab stop, never mouse-only. */}
          <div className="flex shrink-0 items-center motion-safe:transition-opacity [@media(hover:hover)]:opacity-0 group-focus-within/session:opacity-100 group-hover/session:opacity-100">
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
