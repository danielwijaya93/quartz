import { describe, expect, it } from "vitest";
import {
  ARCHIVE_REASONS,
  archiveEntries,
  archiveReasonCounts,
  archiveReasonLabel,
  currentWorkspaceCwd,
  directoryLabel,
  displayTitle,
  EMPTY_SIDEBAR,
  filterArchiveEntries,
  folderBadge,
  formatAge,
  groupArchive,
  groupSessions,
  isArchiveFilterActive,
  isVisible,
  NO_ARCHIVE_FILTER,
  parseSidebarPayload,
  sameFolder,
  type ArchiveEntry,
  type ArchiveReason,
  type LiveSession,
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

describe("sameFolder", () => {
  it("matches the live session to its folder, ignoring a trailing slash", () => {
    expect(sameFolder("/home/x/project", "/home/x/project")).toBe(true);
    expect(sameFolder("/home/x/project/", "/home/x/project")).toBe(true);
    expect(sameFolder("/home/x/project", "/home/x/project/")).toBe(true);
  });

  it("does not match a different folder, or one side that is empty", () => {
    expect(sameFolder("/home/x/project", "/home/x/projects")).toBe(false);
    expect(sameFolder("/home/x/project", "")).toBe(false);
    expect(sameFolder("", "")).toBe(false);
  });
});

describe("currentWorkspaceCwd", () => {
  function groupsFor(sessions: SidebarSession[], workspaces: SidebarPayload["sidebar"]["workspaces"] = []) {
    return groupSessions(payload({ sessions, sidebar: { ...EMPTY_SIDEBAR, workspaces } }));
  }

  it("names the live session's folder, even when the reported cwd disagrees", () => {
    // The reported bug: with a session in `alpha-research` selected, the server's
    // start folder and the session's own folder were both tinted. The session's row
    // is the stronger fact -- it arrived with the folder -- so it wins outright.
    const groups = groupsFor([
      session({ sessionId: "live", cwd: "/home/x/alpha-research" }),
      session({ sessionId: "other", cwd: "/home/x/start-folder" }),
    ]);
    const live: LiveSession = { sessionId: "live", cwd: "/home/x/start-folder" };

    expect(currentWorkspaceCwd(groups, live)).toBe("/home/x/alpha-research");
  });

  it("falls back to the reported folder only before the session has a row", () => {
    const groups = groupsFor([], [{ cwd: "/home/x/fresh", label: "fresh", cwdExists: true }]);

    expect(currentWorkspaceCwd(groups, { sessionId: "brand-new", cwd: "/home/x/fresh/" })).toBe(
      "/home/x/fresh",
    );
  });

  it("answers with a group's own cwd, so at most one group can match it", () => {
    // Two hand-added folders that differ only by a trailing slash must not both
    // compare equal to the answer: the return value is one of them, verbatim.
    const groups = [
      { cwd: "/home/x/project", label: "project", sessions: [] },
      { cwd: "/home/x/project/", label: "project", sessions: [] },
    ];
    const current = currentWorkspaceCwd(groups, { sessionId: "", cwd: "/home/x/project" });

    expect(groups.filter((group) => group.cwd === current)).toHaveLength(1);
  });

  it("says nothing when neither fact names a folder on screen", () => {
    const groups = groupsFor([session({ sessionId: "s1", cwd: "/home/x/project" })]);

    expect(currentWorkspaceCwd(groups, { sessionId: "", cwd: "" })).toBeNull();
    expect(currentWorkspaceCwd(groups, { sessionId: "", cwd: "/home/x/elsewhere" })).toBeNull();
    expect(currentWorkspaceCwd([], { sessionId: "s1", cwd: "/home/x/project" })).toBeNull();
  });
});

describe("folderBadge", () => {
  const fresh = { cwd: "/home/x/fresh", label: "fresh", sessions: [] };

  it("says new only for the live folder whose session has no row yet", () => {
    expect(folderBadge(fresh, { sessionId: "live", cwd: "/home/x/fresh" }, [])).toBe("new");
  });

  it("stops saying new once that session has a message", () => {
    // A log on disk is what "has a message" means here: it is the payload's own row.
    const sessions = [session({ sessionId: "live", cwd: "/home/x/elsewhere" })];
    expect(folderBadge(fresh, { sessionId: "live", cwd: "/home/x/fresh" }, sessions)).toBe("empty");
  });

  it("says empty for any other folder, so an unused one stops reading as onboarding", () => {
    const old = { cwd: "/home/x/old", label: "old", sessions: [] };
    expect(folderBadge(old, { sessionId: "live", cwd: "/home/x/fresh" }, [])).toBe("empty");
    expect(folderBadge(old, { sessionId: "", cwd: "" }, [])).toBe("empty");
  });

  it("says nothing at all for a folder that has sessions", () => {
    const used = { cwd: "/home/x/project", label: "project", sessions: [session()] };
    expect(folderBadge(used, { sessionId: "s1", cwd: "/home/x/project" }, [session()])).toBeNull();
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

describe("archive reasoning, filtering and grouping", () => {
  /** The rows a dialog would draw, from the same payload the sidebar gets. */
  function entriesFrom(
    sessions: SidebarSession[],
    sidebar: Partial<SidebarPayload["sidebar"]> = {},
  ): ArchiveEntry[] {
    return archiveEntries(payload({ sessions, sidebar: { ...EMPTY_SIDEBAR, ...sidebar } }));
  }

  const HIDDEN = session({ sessionId: "hidden-one", cwd: "/home/x/alpha", title: "deploy notes" });
  const TERMINAL = session({
    sessionId: "terminal-one",
    cwd: "/home/x/beta",
    title: "script run",
    origin: "cli",
    updatedAt: "2026-01-02T05:00:00Z",
  });
  const ORPHAN = session({
    sessionId: "orphan",
    cwd: "/home/x/gone-worktree",
    title: "old branch work",
    cwdExists: false,
    updatedAt: "2026-01-02T04:00:00Z",
  });
  const ALL = entriesFrom([HIDDEN, TERMINAL, ORPHAN], {
    hiddenSessions: ["hidden-one"],
  });

  it("gives every row a reason, and the reason is what a filter chip asks about", () => {
    expect(ALL.map((entry) => entry.reasons)).toEqual([
      ["hidden"],
      ["terminal"],
      ["missing-folder"],
    ]);
  });

  it("collects several reasons on one row without inventing an order", () => {
    const [both] = entriesFrom(
      [session({ sessionId: "both", origin: "cli", cwdExists: false, title: "both" })],
      { hiddenSessions: ["both"] },
    );
    // The same order the chips are drawn in, so "why is this here" reads left to right.
    expect(both?.reasons).toEqual([...ARCHIVE_REASONS]);
  });

  it("counts each reason once per row, for the chip labels", () => {
    const counts = archiveReasonCounts(ALL);
    expect(counts).toEqual({ hidden: 1, terminal: 1, "missing-folder": 1 });
  });

  it("filters by title and by folder, case-insensitively", () => {
    const byTitle = filterArchiveEntries(ALL, { query: "DEPLOY", reasons: [] }, EMPTY_SIDEBAR);
    expect(byTitle.map((entry) => entry.session.sessionId)).toEqual(["hidden-one"]);

    // "where was that worktree thing" is a folder question, not a title one.
    const byFolder = filterArchiveEntries(ALL, { query: "gone-worktree", reasons: [] }, EMPTY_SIDEBAR);
    expect(byFolder.map((entry) => entry.session.sessionId)).toEqual(["orphan"]);
  });

  it("filters by a rename, because that is the name the person gave it", () => {
    const entries = entriesFrom([HIDDEN], {
      hiddenSessions: ["hidden-one"],
      renames: { "hidden-one": "Prod rollout" },
    });
    const found = filterArchiveEntries(
      entries,
      { query: "prod", reasons: [] },
      { ...EMPTY_SIDEBAR, renames: { "hidden-one": "Prod rollout" } },
    );
    expect(found).toHaveLength(1);
  });

  it("treats reasons as OR, so a row that is hidden and orphaned shows in both views", () => {
    const both = session({ sessionId: "both", cwd: "/home/x/gone", cwdExists: false });
    const entries = entriesFrom([both, TERMINAL], { hiddenSessions: ["both"] });

    const hidden = filterArchiveEntries(entries, { query: "", reasons: ["hidden"] }, EMPTY_SIDEBAR);
    const missing = filterArchiveEntries(
      entries,
      { query: "", reasons: ["missing-folder"] },
      EMPTY_SIDEBAR,
    );
    expect(hidden.map((entry) => entry.session.sessionId)).toEqual(["both"]);
    expect(missing.map((entry) => entry.session.sessionId)).toEqual(["both"]);
  });

  it("keeps everything when nothing is filtered, and nothing when both filters miss", () => {
    expect(filterArchiveEntries(ALL, NO_ARCHIVE_FILTER, EMPTY_SIDEBAR)).toHaveLength(3);
    expect(isArchiveFilterActive(NO_ARCHIVE_FILTER)).toBe(false);

    const nothing = filterArchiveEntries(
      ALL,
      { query: "no such session", reasons: ["missing-folder"] },
      EMPTY_SIDEBAR,
    );
    expect(nothing).toEqual([]);
  });

  it("treats a whitespace-only query as no query rather than as a filter that hides all", () => {
    expect(isArchiveFilterActive({ query: "   ", reasons: [] })).toBe(false);
    expect(filterArchiveEntries(ALL, { query: "   ", reasons: [] }, EMPTY_SIDEBAR)).toHaveLength(3);
  });

  it("groups rows by folder, newest folder first and rows already newest first", () => {
    const older = session({
      sessionId: "older-in-alpha",
      cwd: "/home/x/alpha",
      title: "older",
      updatedAt: "2026-01-01T00:00:00Z",
      origin: "cli",
    });
    const entries = entriesFrom([older, HIDDEN, TERMINAL], { hiddenSessions: ["hidden-one"] });
    const groups = groupArchive(entries);

    expect(groups.map((group) => group.label)).toEqual(["alpha", "beta"]);
    expect(groups[0]?.entries.map((entry) => entry.session.sessionId)).toEqual([
      "hidden-one",
      "older-in-alpha",
    ]);
  });

  it("marks a group whose folder is gone, so the section explains itself once", () => {
    const groups = groupArchive(ALL);
    const orphan = groups.find((group) => group.label === "gone-worktree");
    expect(orphan?.folderMissing).toBe(true);
    expect(groups.find((group) => group.label === "alpha")?.folderMissing).toBe(false);
  });

  it("names a reason for the chip in words a person reads", () => {
    const labels: Record<ArchiveReason, string> = {
      hidden: archiveReasonLabel("hidden"),
      terminal: archiveReasonLabel("terminal"),
      "missing-folder": archiveReasonLabel("missing-folder"),
    };
    expect(labels).toEqual({
      hidden: "Hidden",
      terminal: "Terminal",
      "missing-folder": "Folder removed",
    });
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
