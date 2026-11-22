/**
 * The folder picker's model: filtering the current directory, and the few string
 * shapes a breadcrumb needs.
 *
 * There is no path parsing here on purpose. `docs/web-protocol.md` says the picker
 * is tap-through only -- a directory list plus breadcrumbs, never a path box -- so
 * the only text a person can type narrows *this* listing, and the only paths the
 * client sends are ones the server handed it.
 */

export interface DirectoryEntry {
  name: string;
  /** The child's full path, as the server reported it. Never assembled here. */
  path: string;
  isDir: boolean;
}

export interface Breadcrumb {
  name: string;
  /** Full path, because that is what `/api/fs/list` accepts. */
  path: string;
}

export interface DirectoryListing {
  path: string;
  parents: Breadcrumb[];
  entries: DirectoryEntry[];
  /** Non-null when the path could not be read. The picker shows it and stays put. */
  error: string | null;
}

/** Where the picker lands on a bad response: nothing to show, nothing to lose. */
export const EMPTY_LISTING: DirectoryListing = {
  path: "",
  parents: [],
  entries: [],
  error: null,
};

function asRecord(value: unknown, what: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`Malformed ${what}`);
  }
  return value as Record<string, unknown>;
}

/** Validate a listing response, so a bad body becomes an error rather than a blank list. */
export function parseListing(raw: unknown): DirectoryListing {
  const body = asRecord(raw, "directory listing");
  // Every listing carries the path it describes, even the failed ones. Without it
  // the body is some other endpoint's answer, and rendering it would show an empty
  // folder that does not exist.
  if (typeof body.path !== "string") {
    throw new Error("Malformed directory listing: no path");
  }
  const parents = Array.isArray(body.parents)
    ? body.parents.map((item): Breadcrumb => {
        const crumb = asRecord(item, "breadcrumb");
        return {
          name: typeof crumb.name === "string" ? crumb.name : "",
          path: typeof crumb.path === "string" ? crumb.path : "",
        };
      })
    : [];
  const entries = Array.isArray(body.entries)
    ? body.entries
        .map((item): DirectoryEntry => {
          const entry = asRecord(item, "directory entry");
          return {
            name: typeof entry.name === "string" ? entry.name : "",
            path: typeof entry.path === "string" ? entry.path : "",
            isDir: entry.isDir !== false,
          };
        })
        // Only directories are ever offered: the API sends nothing else, and a file
        // row in a folder picker would be a button that cannot work.
        .filter((entry) => entry.name !== "" && entry.path !== "" && entry.isDir)
    : [];
  return {
    path: body.path,
    parents,
    entries,
    error: typeof body.error === "string" && body.error !== "" ? body.error : null,
  };
}

/**
 * Narrow the current directory's entries as you type.
 *
 * A filter, not a path box: it never leaves the directory on screen, which is why
 * a name with a slash in it simply matches nothing. Case-insensitive substring
 * rather than fuzzy, because a folder list is short and a fuzzy match that reorders
 * rows as you type makes tapping unreliable.
 */
export function filterDirectories(
  entries: readonly DirectoryEntry[],
  query: string,
): DirectoryEntry[] {
  const needle = query.trim().toLowerCase();
  if (needle === "") return [...entries];
  return entries.filter((entry) => entry.name.toLowerCase().includes(needle));
}

/**
 * The parent directory of a listing, from its own breadcrumbs.
 *
 * Returns null at the root, so "up" is a disabled control rather than a request for
 * the parent of `/`.
 */
export function parentPath(listing: DirectoryListing): string | null {
  const parents = listing.parents;
  if (parents.length < 2) return null;
  return parents[parents.length - 2]?.path ?? null;
}

/** The last breadcrumb is the directory on screen; the header names it. */
export function currentName(listing: DirectoryListing): string {
  const last = listing.parents[listing.parents.length - 1];
  if (last?.name) return last.name;
  const parts = listing.path.split("/").filter(Boolean);
  return parts[parts.length - 1] ?? "/";
}
