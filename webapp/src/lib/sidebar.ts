/**
 * The sidebar's model: what a session row is, which rows belong on screen, and
 * how they group into folders.
 *
 * Pure and framework-free, like `transcript.ts`, and separate from it on purpose:
 * the transcript is what a turn produced, the sidebar is which sessions exist and
 * which of them the person wants to see. Mixing the two is how a "hide" turns into
 * a filter over rows that were never about sessions.
 *
 * The visibility rule is the server's rule (`quartz/web/sidebar.py`), restated
 * here because the client filters a payload it already has: sending every session
 * and the whole sidebar state in one response is what lets the archive dialog be
 * built from the difference, without a second request.
 */

/** Which transport created a session. Old logs have no origin; the server says `cli`. */
export type SessionOrigin = "web" | "cli";

/** One stored session, as `GET /api/sessions` describes it. */
export interface SidebarSession {
  sessionId: string;
  cwd: string;
  /** The first-user-message preview, absent for a session with no messages. */
  title?: string;
  /** ISO timestamp of the transcript's mtime. */
  updatedAt: string;
  origin: SessionOrigin;
  /** False when the folder has been removed (a worktree, usually). */
  cwdExists: boolean;
}

/** A folder added by hand, so an empty one can still be shown and used. */
export interface SidebarWorkspace {
  cwd: string;
  label: string;
  cwdExists: boolean;
}

export interface SidebarState {
  version: number;
  hiddenSessions: string[];
  importedSessions: string[];
  pinnedSessions: string[];
  renames: Record<string, string>;
  workspaces: SidebarWorkspace[];
}

/** The body of every sidebar endpoint. */
export interface SidebarPayload {
  sessions: SidebarSession[];
  sidebar: SidebarState;
}

export const EMPTY_SIDEBAR: SidebarState = {
  version: 1,
  hiddenSessions: [],
  importedSessions: [],
  pinnedSessions: [],
  renames: {},
  workspaces: [],
};

/* ------------------------------------------------------------------ parsing */

function asRecord(value: unknown, what: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`Malformed ${what}`);
  }
  return value as Record<string, unknown>;
}

function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function parseSession(raw: unknown): SidebarSession {
  const record = asRecord(raw, "session");
  const origin = record.origin === "web" ? "web" : "cli";
  return {
    sessionId: asString(record.sessionId),
    cwd: asString(record.cwd),
    ...(typeof record.title === "string" ? { title: record.title } : {}),
    updatedAt: asString(record.updatedAt),
    origin,
    cwdExists: record.cwdExists !== false,
  };
}

/**
 * Turn a response body into a `SidebarPayload`, or throw.
 *
 * Validating here rather than trusting the shape is what keeps a half-migrated
 * server from rendering an empty sidebar: a payload we cannot understand becomes an
 * error with a retry, which is honest, instead of "you have no sessions".
 */
export function parseSidebarPayload(raw: unknown): SidebarPayload {
  const body = asRecord(raw, "sessions response");
  const state = asRecord(body.sidebar ?? {}, "sidebar state");
  const renames: Record<string, string> = {};
  const rawRenames = asRecord(state.renames ?? {}, "renames");
  for (const [key, value] of Object.entries(rawRenames)) {
    if (typeof value === "string" && value.trim() !== "") renames[key] = value.trim();
  }
  const workspaces = Array.isArray(state.workspaces)
    ? state.workspaces.map((item): SidebarWorkspace => {
        const workspace = asRecord(item, "workspace");
        const cwd = asString(workspace.cwd);
        return {
          cwd,
          label: asString(workspace.label) || directoryLabel(cwd),
          cwdExists: workspace.cwdExists !== false,
        };
      })
    : [];
  if (!Array.isArray(body.sessions)) {
    throw new Error("Malformed sessions response: no sessions array");
  }
  return {
    sessions: body.sessions.map(parseSession).filter((session) => session.sessionId !== ""),
    sidebar: {
      version: typeof state.version === "number" ? state.version : 1,
      hiddenSessions: asStringArray(state.hiddenSessions),
      importedSessions: asStringArray(state.importedSessions),
      pinnedSessions: asStringArray(state.pinnedSessions),
      renames,
      workspaces,
    },
  };
}

/* --------------------------------------------------------------- visibility */

/**
 * The rule: show a session when it came from the web or was imported, unless it is
 * hidden. Hidden wins, which is what makes Restore in the archive meaningful.
 */
export function isVisible(session: SidebarSession, sidebar: SidebarState): boolean {
  if (sidebar.hiddenSessions.includes(session.sessionId)) return false;
  return session.origin === "web" || sidebar.importedSessions.includes(session.sessionId);
}

/** A rename wins over the preview; an empty log gets a name rather than a blank row. */
export function displayTitle(session: SidebarSession, sidebar: SidebarState): string {
  const renamed = sidebar.renames[session.sessionId];
  if (renamed) return renamed;
  const preview = session.title?.trim();
  if (preview) return preview;
  return "Untitled session";
}

/* ----------------------------------------------------------------- grouping */

export interface WorkspaceGroup {
  cwd: string;
  label: string;
  /** Visible sessions in this folder, newest first. */
  sessions: SidebarSession[];
}

/** Last path segment, which is what a person calls the folder. */
export function directoryLabel(cwd: string): string {
  const parts = cwd.split("/").filter(Boolean);
  return parts[parts.length - 1] ?? cwd;
}

function withoutTrailingSlash(cwd: string): string {
  return cwd.replace(/\/+$/, "") || cwd;
}

/**
 * Whether two `cwd` values name the same folder.
 *
 * The sidebar marks the folder the live session is running in, and the two paths
 * being compared come from different frames: `session_state` and the workspace
 * list. Both are server-resolved, so this only has to forgive a trailing slash --
 * but forgiving it here beats a highlight that silently does not appear.
 */
export function sameFolder(a: string, b: string): boolean {
  if (!a || !b) return false;
  return withoutTrailingSlash(a) === withoutTrailingSlash(b);
}

/* ----------------------------------------------------------------- notices */

/**
 * How long an undoable confirmation stays on screen.
 *
 * Long enough to read a sentence and reach Undo with a mouse, short enough that it
 * is gone before it becomes furniture. It is held while the keyboard is inside it,
 * so this is a ceiling rather than a deadline for someone already on their way.
 */
export const NOTICE_TIMEOUT_MS = 6000;

/* --------------------------------------------------------- current workspace */

/**
 * Where the live session is, as two independent facts.
 *
 * They come from different places -- `session_state` for the folder, the sessions
 * payload for the row -- and they can disagree for a moment (a resumed session may
 * still report the server's start folder). Keeping them together here is what lets
 * the sidebar decide which one to trust, once.
 */
export interface LiveSession {
  /** Empty until the server has assigned an id. */
  sessionId: string;
  /** The folder the next prompt lands in, per `session_state`. */
  cwd: string;
}

/**
 * The one workspace the next prompt will land in, or null.
 *
 * The bug this exists to prevent: a live session in `alpha-research` while
 * `session_state` still reported the server's start folder tinted *both* folders,
 * so two rows claimed to be where you were. A person can only be in one folder, so
 * this returns at most one answer, and it prefers the live session's own row -- that
 * row and its folder arrived together from the server, while `cwd` is a second
 * opinion. Only when there is no row yet (a session started but not prompted) does
 * `cwd` decide.
 *
 * The returned value is always some group's own `cwd`, so callers compare with `===`
 * and cannot end up tinting two groups that fail to compare equal because of a
 * trailing slash.
 */
export function currentWorkspaceCwd(groups: readonly WorkspaceGroup[], live: LiveSession): string | null {
  if (live.sessionId !== "") {
    const owner = groups.find((group) =>
      group.sessions.some((session) => session.sessionId === live.sessionId),
    );
    if (owner) return owner.cwd;
  }
  if (!live.cwd) return null;
  return groups.find((group) => sameFolder(group.cwd, live.cwd))?.cwd ?? null;
}

/* ------------------------------------------------------------- folder state */

/** What an empty folder's badge may say. A folder with sessions says neither. */
export type FolderBadge = "new" | "empty";

/**
 * Whether an empty folder is genuinely new, or just empty.
 *
 * "NEW" is a promise: this folder is where the thing you just started is about to
 * appear. It holds only until that session has a log, and a log means a row
 * somewhere in the payload -- which is why the whole session list is a parameter
 * rather than just the group. Before that the folder would be labelled NEW forever,
 * which after a week of use reads as an onboarding step that was never finished.
 */
export function folderBadge(
  group: WorkspaceGroup,
  live: LiveSession,
  sessions: readonly SidebarSession[],
): FolderBadge | null {
  if (group.sessions.length > 0) return null;
  const started =
    live.sessionId !== "" && sessions.some((session) => session.sessionId === live.sessionId);
  return started || !sameFolder(group.cwd, live.cwd) ? "empty" : "new";
}

function timestamp(iso: string): number {
  const value = Date.parse(iso);
  return Number.isFinite(value) ? value : 0;
}

/**
 * Fold the payload into the two-level list the sidebar draws: one group per folder,
 * even an empty one, sessions newest first.
 *
 * Folders with sessions come first, then empty ones alphabetically. That ordering
 * is a product decision, not an accident: the thing you used yesterday should not
 * be pushed down by a folder you added once and never opened.
 *
 * A folder whose directory is gone is dropped here rather than dimmed. It cannot
 * start a session and cannot hold a new one, so on a first run -- before anything
 * has been started from the browser -- the list would otherwise be sixteen dead
 * paths and no way to begin. Those folders are surfaced by `archiveEntries`
 * instead, which is the one place acting on a missing path makes sense.
 */
export function groupSessions(payload: SidebarPayload): WorkspaceGroup[] {
  const visible = payload.sessions.filter((session) => isVisible(session, payload.sidebar));
  const groups = new Map<string, WorkspaceGroup>();
  const gone = new Set<string>();

  for (const workspace of payload.sidebar.workspaces) {
    groups.set(workspace.cwd, {
      cwd: workspace.cwd,
      label: workspace.label || directoryLabel(workspace.cwd),
      sessions: [],
    });
    if (!workspace.cwdExists) gone.add(workspace.cwd);
  }

  for (const session of visible) {
    // Either side saying the folder is gone is enough: both are the server's own
    // `is_dir` at request time, and hiding one folder too many beats showing a row
    // that cannot be used.
    if (!session.cwdExists) gone.add(session.cwd);
    const existing = groups.get(session.cwd);
    if (existing) {
      existing.sessions.push(session);
      continue;
    }
    groups.set(session.cwd, {
      cwd: session.cwd,
      label: directoryLabel(session.cwd),
      sessions: [session],
    });
  }

  const list = [...groups.values()].filter((group) => !gone.has(group.cwd));
  for (const group of list) {
    group.sessions.sort((a, b) => timestamp(b.updatedAt) - timestamp(a.updatedAt));
  }
  list.sort((a, b) => {
    const aEmpty = a.sessions.length === 0;
    const bEmpty = b.sessions.length === 0;
    if (aEmpty !== bEmpty) {
      return a.sessions.length === 0 ? 1 : -1;
    }
    const aLatest = a.sessions[0] ? timestamp(a.sessions[0].updatedAt) : 0;
    const bLatest = b.sessions[0] ? timestamp(b.sessions[0].updatedAt) : 0;
    if (aLatest !== bLatest) return bLatest - aLatest;
    return a.label.localeCompare(b.label);
  });
  return list;
}

/* ----------------------------------------------------------------- archive */

/**
 * Why a row is in "Not in sidebar", as a small closed set.
 *
 * A row can be here for more than one reason -- a terminal session that was also
 * hidden needs an import *and* a restore -- so this is a list. It is also the thing
 * the dialog filters by: with ~150 rows, the reason a row exists is worth saying
 * once per filter instead of 148 times per row.
 */
export const ARCHIVE_REASONS = ["hidden", "terminal", "missing-folder"] as const;

export type ArchiveReason = (typeof ARCHIVE_REASONS)[number];

/** The words on a reason chip. Short enough to sit all three in one row. */
export function archiveReasonLabel(reason: ArchiveReason): string {
  switch (reason) {
    case "hidden":
      return "Hidden";
    case "terminal":
      return "Terminal";
    case "missing-folder":
      return "Folder removed";
  }
}

/** One row of the "Not in sidebar" dialog, and what it would take to list it. */
export interface ArchiveEntry {
  session: SidebarSession;
  /** In `hiddenSessions`: a Restore is needed. */
  hidden: boolean;
  /** Not a web session and not imported: an Import is needed. */
  needsImport: boolean;
  /** The folder is gone, so the sidebar cannot list this session at all. */
  missingFolder: boolean;
  /** The same three facts as `ARCHIVE_REASONS`, in the order the chips are drawn. */
  reasons: ArchiveReason[];
}

/**
 * The other half of archiving: everything the sidebar is not showing.
 *
 * A row is here for one of two different reasons, and the row has to say which. It
 * may be invisible by rule -- a terminal session never imported, a web session that
 * was hidden -- and the dialog is the only way back for either. Or its folder is
 * gone, which the sidebar does not draw at all: those sessions are still real
 * transcripts, so they are listed here where a person can see them and act.
 *
 * Newest first, because the one you want is almost always the one you just lost.
 */
export function archiveEntries(payload: SidebarPayload): ArchiveEntry[] {
  return payload.sessions
    .filter((session) => !session.cwdExists || !isVisible(session, payload.sidebar))
    .map((session) => {
      const hidden = payload.sidebar.hiddenSessions.includes(session.sessionId);
      const needsImport =
        session.origin !== "web" && !payload.sidebar.importedSessions.includes(session.sessionId);
      const missingFolder = !session.cwdExists;
      const reasons = ARCHIVE_REASONS.filter(
        (reason) =>
          (reason === "hidden" && hidden) ||
          (reason === "terminal" && needsImport) ||
          (reason === "missing-folder" && missingFolder),
      );
      return { session, hidden, needsImport, missingFolder, reasons };
    })
    .sort((a, b) => timestamp(b.session.updatedAt) - timestamp(a.session.updatedAt));
}

/**
 * What the dialog's filter says: free text, plus any reason chips that are on.
 *
 * Two independent controls over the same list, because the two questions are
 * different: "where is that session about the deploy" is text, "what can I still
 * restore" is a reason. An empty reason set means no reason filter rather than no
 * rows -- a chip that is off must not hide everything.
 */
export interface ArchiveFilter {
  query: string;
  reasons: readonly ArchiveReason[];
}

export const NO_ARCHIVE_FILTER: ArchiveFilter = { query: "", reasons: [] };

/** Whether a filter would change anything, so the dialog can offer to clear it. */
export function isArchiveFilterActive(filter: ArchiveFilter): boolean {
  return filter.query.trim() !== "" || filter.reasons.length > 0;
}

/**
 * The rows the dialog should draw.
 *
 * The text matches the title (a rename included, since that is the name the person
 * gave the session) and the folder, because with ~150 rows the folder is half of
 * how anyone remembers a session. Reasons are OR-ed: a row that is hidden and whose
 * folder is gone belongs in both views.
 */
export function filterArchiveEntries(
  entries: readonly ArchiveEntry[],
  filter: ArchiveFilter,
  sidebar: SidebarState,
): ArchiveEntry[] {
  const needle = filter.query.trim().toLowerCase();
  return entries.filter((entry) => {
    if (
      filter.reasons.length > 0 &&
      !entry.reasons.some((reason) => filter.reasons.includes(reason))
    ) {
      return false;
    }
    if (needle === "") return true;
    if (displayTitle(entry.session, sidebar).toLowerCase().includes(needle)) return true;
    return entry.session.cwd.toLowerCase().includes(needle);
  });
}

/** One folder's worth of archive rows. */
export interface ArchiveGroup {
  cwd: string;
  label: string;
  /** Every session in it is unreachable, so the section says why once. */
  folderMissing: boolean;
  entries: ArchiveEntry[];
}

/**
 * Group the rows by folder.
 *
 * A flat list of 150 rows is a wall; a dozen folders with a heading each is
 * scannable, and it is the same grouping the sidebar itself uses. Groups appear in
 * the order their newest session does, so the thing you just hid is still at the
 * top.
 */
export function groupArchive(entries: readonly ArchiveEntry[]): ArchiveGroup[] {
  const groups = new Map<string, ArchiveGroup>();
  for (const entry of entries) {
    const existing = groups.get(entry.session.cwd);
    if (existing) {
      existing.entries.push(entry);
      continue;
    }
    groups.set(entry.session.cwd, {
      cwd: entry.session.cwd,
      label: directoryLabel(entry.session.cwd),
      folderMissing: entry.missingFolder,
      entries: [entry],
    });
  }
  return [...groups.values()];
}

/** How many rows each reason explains, for the chip labels. */
export function archiveReasonCounts(
  entries: readonly ArchiveEntry[],
): Record<ArchiveReason, number> {
  const counts: Record<ArchiveReason, number> = { hidden: 0, terminal: 0, "missing-folder": 0 };
  for (const entry of entries) {
    for (const reason of entry.reasons) counts[reason] += 1;
  }
  return counts;
}

/* --------------------------------------------------------------------- age */

/**
 * Compact age: `6h`, `2d`. Same shape the reference sidebar uses.
 *
 * Seconds are deliberately collapsed to `now`: a row that changes every time you
 * look at it is noise, and nothing here needs that resolution.
 */
export function formatAge(iso: string, now: number = Date.now()): string {
  const ts = timestamp(iso);
  if (!ts) return "";
  const minutes = Math.max(0, Math.floor((now - ts) / 60_000));
  if (minutes < 60) return "now";
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d`;
  return `${Math.floor(days / 30)}mo`;
}

/* ----------------------------------------------------- search & live status */

export type SessionActivityStatus = "idle" | "running" | "done" | "error" | "permission";

export interface SessionActivity {
  status: SessionActivityStatus;
  unread: boolean;
}

/**
 * Filter workspace groups by a free-text query across folder labels, paths, and
 * session titles. When a folder itself matches, all of its sessions remain visible;
 * otherwise only matching sessions are kept and empty groups are omitted.
 */
export function filterWorkspaceGroups(
  groups: readonly WorkspaceGroup[],
  query: string,
  sidebar: SidebarState,
): WorkspaceGroup[] {
  const needle = query.trim().toLowerCase();
  if (needle === "") return [...groups];

  const filtered: WorkspaceGroup[] = [];
  for (const group of groups) {
    const folderMatches =
      group.label.toLowerCase().includes(needle) ||
      group.cwd.toLowerCase().includes(needle);
    if (folderMatches) {
      filtered.push(group);
      continue;
    }
    const matchingSessions = group.sessions.filter((session) =>
      displayTitle(session, sidebar).toLowerCase().includes(needle),
    );
    if (matchingSessions.length > 0) {
      filtered.push({ ...group, sessions: matchingSessions });
    }
  }
  return filtered;
}

/**
 * Visible sessions that the user pinned, newest first.
 *
 * Hidden sessions and sessions whose folder no longer exists are excluded,
 * matching `groupSessions` so the sidebar never shows an unreachable row.
 */
export function pinnedSessions(payload: SidebarPayload, query = ""): SidebarSession[] {
  const order = new Map<string, number>();
  payload.sidebar.pinnedSessions.forEach((id, index) => {
    if (!order.has(id)) order.set(id, index);
  });
  if (order.size === 0) return [];
  const needle = query.trim().toLowerCase();
  return payload.sessions
    .filter((session) => {
      if (!order.has(session.sessionId)) return false;
      if (!session.cwdExists) return false;
      if (!isVisible(session, payload.sidebar)) return false;
      if (needle === "") return true;
      return (
        displayTitle(session, payload.sidebar).toLowerCase().includes(needle) ||
        directoryLabel(session.cwd).toLowerCase().includes(needle) ||
        session.cwd.toLowerCase().includes(needle)
      );
    })
    .sort((a, b) => (order.get(a.sessionId) ?? 0) - (order.get(b.sessionId) ?? 0));
}

/* ----------------------------------------------------------- collapse state */

export const SIDEBAR_COLLAPSE_STORAGE_KEY = "quartz-sidebar-collapse";

export interface SidebarCollapseState {
  folders: Record<string, boolean>;
  pinned: boolean;
}

export const DEFAULT_SIDEBAR_COLLAPSE_STATE: SidebarCollapseState = {
  folders: {},
  pinned: true,
};

function defaultCollapseState(): SidebarCollapseState {
  return { folders: {}, pinned: true };
}

/**
 * Read the persisted folder and Pinned expand/collapse preferences.
 * Never throws; returns defaults when storage is absent, blocked, or malformed.
 */
export function readSidebarCollapseState(
  storage?: Pick<Storage, "getItem">,
): SidebarCollapseState {
  const target = storage ?? (typeof localStorage === "undefined" ? undefined : localStorage);
  if (!target) return defaultCollapseState();
  try {
    const raw = target.getItem(SIDEBAR_COLLAPSE_STORAGE_KEY);
    if (!raw) return defaultCollapseState();
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return defaultCollapseState();
    }
    const record = parsed as Record<string, unknown>;
    const folders: Record<string, boolean> = {};
    if (typeof record.folders === "object" && record.folders !== null && !Array.isArray(record.folders)) {
      for (const [cwd, expanded] of Object.entries(record.folders as Record<string, unknown>)) {
        if (typeof expanded === "boolean") folders[cwd] = expanded;
      }
    }
    return {
      folders,
      pinned: typeof record.pinned === "boolean" ? record.pinned : true,
    };
  } catch {
    return defaultCollapseState();
  }
}

/**
 * Persist the folder and Pinned expand/collapse preferences.
 * A blocked or full store is silently ignored so the UI continues to work.
 */
export function writeSidebarCollapseState(
  state: SidebarCollapseState,
  storage?: Pick<Storage, "setItem">,
): void {
  const target = storage ?? (typeof localStorage === "undefined" ? undefined : localStorage);
  if (!target) return;
  try {
    target.setItem(SIDEBAR_COLLAPSE_STORAGE_KEY, JSON.stringify(state));
  } catch {
    // Quota or private-mode rejection. The in-memory choice still applies.
  }
}
