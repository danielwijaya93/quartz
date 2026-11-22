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

/** One row of the "Not in sidebar" dialog, and what it would take to list it. */
export interface ArchiveEntry {
  session: SidebarSession;
  /** In `hiddenSessions`: a Restore is needed. */
  hidden: boolean;
  /** Not a web session and not imported: an Import is needed. */
  needsImport: boolean;
  /** The folder is gone, so the sidebar cannot list this session at all. */
  missingFolder: boolean;
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
    .map((session) => ({
      session,
      hidden: payload.sidebar.hiddenSessions.includes(session.sessionId),
      needsImport:
        session.origin !== "web" && !payload.sidebar.importedSessions.includes(session.sessionId),
      missingFolder: !session.cwdExists,
    }))
    .sort((a, b) => timestamp(b.session.updatedAt) - timestamp(a.session.updatedAt));
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
