/**
 * The sidebar's HTTP calls, and nothing else.
 *
 * Every endpoint returns the whole `{sessions, sidebar}` body, so a mutation needs
 * no local patching: the component draws what the server just said. That is what
 * makes a reload show the same page as the screen, and it is why nothing here is
 * optimistic.
 *
 * Errors are thrown as `RequestError` with the server's own message, because the
 * sidebar's job in that case is to say what failed and offer a retry -- not to
 * render an empty folder list that looks like "you have no sessions".
 */

import { parseListing, type DirectoryListing } from "./folderPicker";
import { parseSidebarPayload, type SidebarPayload } from "./sidebar";

export class RequestError extends Error {
  readonly retryable: boolean;

  constructor(message: string, retryable = true) {
    super(message);
    this.name = "RequestError";
    this.retryable = retryable;
  }
}

async function readError(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (typeof body === "object" && body !== null) {
      const message = (body as { error?: unknown }).error;
      if (typeof message === "string" && message !== "") return message;
    }
  } catch {
    // A body that is not JSON is not worth reporting; the status is.
  }
  return `${response.status} ${response.statusText}`.trim();
}

async function requestJson(url: string, init?: RequestInit): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(url, init);
  } catch (cause) {
    // `fetch` rejects only on a transport failure: the server is down, or the
    // browser is offline. Both are retryable and both need a sentence a person can
    // act on rather than "TypeError: Failed to fetch".
    throw new RequestError(
      cause instanceof Error && cause.message ? cause.message : "The server is not reachable",
    );
  }
  if (!response.ok) {
    throw new RequestError(await readError(response), response.status >= 500);
  }
  try {
    return await response.json();
  } catch {
    throw new RequestError("The server sent a response that is not JSON");
  }
}

const JSON_HEADERS = { "Content-Type": "application/json" } as const;

/** `GET /api/sessions`: every session plus the whole sidebar state. */
export async function fetchSidebar(signal?: AbortSignal): Promise<SidebarPayload> {
  const body = await requestJson("/api/sessions", signal ? { signal } : undefined);
  return parseSidebarPayload(body);
}

async function post(url: string, body: Record<string, unknown>): Promise<SidebarPayload> {
  const response = await requestJson(url, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  });
  return parseSidebarPayload(response);
}

/** Archive one session from the sidebar. The log on disk is untouched. */
export function hideSession(sessionId: string): Promise<SidebarPayload> {
  return post("/api/sessions/hide", { sessionId });
}

/** Bring an archived session back into the sidebar. */
export function restoreSession(sessionId: string): Promise<SidebarPayload> {
  return post("/api/sessions/restore", { sessionId });
}

/** List a session that was started elsewhere (usually the terminal). */
export function importSession(sessionId: string): Promise<SidebarPayload> {
  return post("/api/sessions/import", { sessionId });
}

/** Pin a session to the top of the sidebar. */
export function pinSession(sessionId: string): Promise<SidebarPayload> {
  return post("/api/sessions/pin", { sessionId });
}

/** Unpin a session from the top of the sidebar. */
export function unpinSession(sessionId: string): Promise<SidebarPayload> {
  return post("/api/sessions/unpin", { sessionId });
}

/** Store a display title. An empty title clears it back to the preview. */
export function renameSession(sessionId: string, title: string): Promise<SidebarPayload> {
  return post("/api/sessions/rename", { sessionId, title });
}

/** Add a folder to the sidebar, so an empty one can be used before it has sessions. */
export function addWorkspace(cwd: string): Promise<SidebarPayload> {
  return post("/api/workspaces", { cwd });
}

/**
 * `GET /api/fs/list`. A bad path comes back as `error` inside the listing rather
 * than as a thrown failure: the picker has to stay on screen and let the person go
 * back up, which a rejected promise cannot express.
 */
export async function listDirectories(path?: string): Promise<DirectoryListing> {
  const query = path === undefined ? "" : `?path=${encodeURIComponent(path)}`;
  const body = await requestJson(`/api/fs/list${query}`);
  return parseListing(body);
}

/** Create one folder inside `path`, then return that directory's fresh listing. */
export async function createDirectory(path: string, name: string): Promise<DirectoryListing> {
  const body = await requestJson("/api/fs/mkdir", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ path, name }),
  });
  return parseListing(body);
}
