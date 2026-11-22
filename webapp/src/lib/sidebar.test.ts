import { describe, expect, it } from "vitest";
import {
  archiveEntries,
  directoryLabel,
  displayTitle,
  EMPTY_SIDEBAR,
  formatAge,
  groupSessions,
  isVisible,
  parseSidebarPayload,
  type SidebarPayload,
  type SidebarSession,
} from "./sidebar";

const NOW = Date.parse("2026-01-02T12:00:00Z");

function session(overrides: Partial<SidebarSession> = {}): SidebarSession {
  return {
    sessionId: "s1",
    cwd: "/home/x/project",
    title: "first prompt",
    updatedAt: "2026-01-02T06:00:00Z",
    origin: "web",
    cwdExists: true,
    ...overrides,
  };
}

function payload(overrides: Partial<SidebarPayload> = {}): SidebarPayload {
  return {
    sessions: [session()],
    sidebar: { ...EMPTY_SIDEBAR, workspaces: [] },
    ...overrides,
  };
}

describe("isVisible", () => {
  it("shows web sessions and hides untouched terminal sessions", () => {
    const state = { ...EMPTY_SIDEBAR };
    expect(isVisible(session(), state)).toBe(true);
    expect(isVisible(session({ origin: "cli" }), state)).toBe(false);
  });

  it("shows an imported terminal session", () => {
    const state = { ...EMPTY_SIDEBAR, importedSessions: ["s1"] };
    expect(isVisible(session({ origin: "cli" }), state)).toBe(true);
  });

  it("lets hidden win over everything, which is what Restore undoes", () => {
    const state = { ...EMPTY_SIDEBAR, importedSessions: ["s1"], hiddenSessions: ["s1"] };
    expect(isVisible(session(), state)).toBe(false);
  });
});

describe("displayTitle", () => {
  it("prefers a rename over the preview", () => {
    const state = { ...EMPTY_SIDEBAR, renames: { s1: "Sidebar work" } };
    expect(displayTitle(session(), state)).toBe("Sidebar work");
  });

  it("falls back to the preview, then to a name", () => {
    expect(displayTitle(session(), EMPTY_SIDEBAR)).toBe("first prompt");
    expect(displayTitle(session({ title: undefined }), EMPTY_SIDEBAR)).toBe("Untitled session");
    expect(displayTitle(session({ title: "   " }), EMPTY_SIDEBAR)).toBe("Untitled session");
  });
});

describe("groupSessions", () => {
  it("groups visible sessions under their folder, newest first", () => {
    const groups = groupSessions(
      payload({
        sessions: [
          session({ sessionId: "old", updatedAt: "2026-01-01T00:00:00Z" }),
          session({ sessionId: "new", updatedAt: "2026-01-02T00:00:00Z" }),
          session({ sessionId: "hidden-one", cwd: "/home/x/other", origin: "cli" }),
        ],
      }),
    );

    expect(groups).toHaveLength(1);
    expect(groups[0]?.label).toBe("project");
    expect(groups[0]?.sessions.map((row) => row.sessionId)).toEqual(["new", "old"]);
  });

  it("keeps a hand-added folder with no sessions, and puts it after used ones", () => {
    const groups = groupSessions(
      payload({
        sessions: [session()],
        sidebar: {
          ...EMPTY_SIDEBAR,
          workspaces: [
            { cwd: "/home/x/empty", label: "empty", cwdExists: true },
            { cwd: "/home/x/project", label: "project", cwdExists: true },
          ],
        },
      }),
    );

    expect(groups.map((group) => group.label)).toEqual(["project", "empty"]);
    expect(groups[1]?.sessions).toEqual([]);
  });

  it("drops a folder whose directory is gone, whether it arrived by hand or with a session", () => {
    const groups = groupSessions(
      payload({
        sessions: [session({ sessionId: "kept" }), session({ sessionId: "dead", cwd: "/home/x/gone", cwdExists: false })],
        sidebar: {
          ...EMPTY_SIDEBAR,
          workspaces: [
            { cwd: "/home/x/project", label: "project", cwdExists: true },
            { cwd: "/home/x/via-workspace", label: "via-workspace", cwdExists: false },
          ],
        },
      }),
    );

    expect(groups.map((group) => group.label)).toEqual(["project"]);
    expect(groups[0]?.sessions.map((row) => row.sessionId)).toEqual(["kept"]);
  });

  it("shows nothing at all on a first run full of dead folders", () => {
    // The state the product review started from: sixteen pytest temp folders, no
    // browser session yet. A folder nobody can start a session in is not a row.
    const groups = groupSessions(
      payload({
        sessions: [],
        sidebar: {
          ...EMPTY_SIDEBAR,
          workspaces: [
            { cwd: "/tmp/pytest-1", label: "pytest-1", cwdExists: false },
            { cwd: "/tmp/pytest-2", label: "pytest-2", cwdExists: false },
          ],
        },
      }),
    );
    expect(groups).toEqual([]);
  });

  it("does not duplicate a folder that is both hand-added and has sessions", () => {
    const groups = groupSessions(
      payload({
        sidebar: {
          ...EMPTY_SIDEBAR,
          workspaces: [{ cwd: "/home/x/project", label: "named by hand", cwdExists: true }],
        },
      }),
    );
    expect(groups).toHaveLength(1);
    expect(groups[0]?.label).toBe("named by hand");
    expect(groups[0]?.sessions).toHaveLength(1);
  });

  it("labels a folder that arrived with sessions", () => {
    expect(directoryLabel("/home/x/project")).toBe("project");
    expect(groupSessions(payload())[0]?.label).toBe("project");
  });
});

describe("archiveEntries", () => {
  it("lists what the sidebar is not showing, with what it would take", () => {
    const entries = archiveEntries(
      payload({
        sessions: [
          session({ sessionId: "web-hidden" }),
          session({ sessionId: "cli-new", origin: "cli", updatedAt: "2026-01-02T09:00:00Z" }),
          session({ sessionId: "web-visible", updatedAt: "2026-01-01T00:00:00Z" }),
        ],
        sidebar: { ...EMPTY_SIDEBAR, hiddenSessions: ["web-hidden"] },
      }),
    );

    expect(entries.map((entry) => entry.session.sessionId)).toEqual(["cli-new", "web-hidden"]);
    expect(entries[0]).toMatchObject({ hidden: false, needsImport: true });
    expect(entries[1]).toMatchObject({ hidden: true, needsImport: false });
  });

  it("keeps a session whose folder is gone reachable, and says why it is here", () => {
    const entries = archiveEntries(
      payload({
        sessions: [
          session({ sessionId: "dead-folder", cwdExists: false }),
          session({ sessionId: "listed" }),
        ],
      }),
    );

    expect(entries.map((entry) => entry.session.sessionId)).toEqual(["dead-folder"]);
    expect(entries[0]).toMatchObject({ missingFolder: true });
  });

  it("asks for both an import and a restore when a terminal session was archived", () => {
    const entries = archiveEntries(
      payload({
        sessions: [session({ origin: "cli" })],
        sidebar: { ...EMPTY_SIDEBAR, hiddenSessions: ["s1"] },
      }),
    );
    expect(entries[0]).toMatchObject({ hidden: true, needsImport: true });
  });
});

describe("formatAge", () => {
  it("uses the compact shapes a sidebar row can carry", () => {
    expect(formatAge("2026-01-02T11:59:30Z", NOW)).toBe("now");
    expect(formatAge("2026-01-02T06:00:00Z", NOW)).toBe("6h");
    expect(formatAge("2025-12-31T12:00:00Z", NOW)).toBe("2d");
    expect(formatAge("2025-11-02T12:00:00Z", NOW)).toBe("2mo");
  });

  it("says nothing for a timestamp it cannot read", () => {
    expect(formatAge("", NOW)).toBe("");
    expect(formatAge("not a date", NOW)).toBe("");
  });
});

describe("parseSidebarPayload", () => {
  it("reads the documented shape", () => {
    const parsed = parseSidebarPayload({
      sessions: [
        {
          sessionId: "s1",
          cwd: "/w",
          title: "hi",
          updatedAt: "2026-01-01T00:00:00Z",
          origin: "web",
          cwdExists: false,
        },
      ],
      sidebar: {
        version: 1,
        hiddenSessions: ["s2"],
        importedSessions: ["s3"],
        renames: { s2: "kept", s4: "  " },
        workspaces: [{ cwd: "/w", label: "w", cwdExists: true }],
      },
    });

    expect(parsed.sessions[0]).toEqual({
      sessionId: "s1",
      cwd: "/w",
      title: "hi",
      updatedAt: "2026-01-01T00:00:00Z",
      origin: "web",
      cwdExists: false,
    });
    expect(parsed.sidebar.hiddenSessions).toEqual(["s2"]);
    expect(parsed.sidebar.renames).toEqual({ s2: "kept" });
  });

  it("treats a missing origin as a terminal session", () => {
    const parsed = parseSidebarPayload({ sessions: [{ sessionId: "s1", cwd: "/w" }], sidebar: {} });
    expect(parsed.sessions[0]?.origin).toBe("cli");
    expect(parsed.sessions[0]?.cwdExists).toBe(true);
  });

  it("throws rather than rendering an empty sidebar for a malformed body", () => {
    expect(() => parseSidebarPayload({ sidebar: {} })).toThrow(/no sessions array/);
    expect(() => parseSidebarPayload(null)).toThrow(/Malformed/);
    expect(() => parseSidebarPayload({ sessions: ["nope"], sidebar: {} })).toThrow(/Malformed/);
  });
});
