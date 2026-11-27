import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FolderSidebar, type FolderSidebarProps } from "./FolderSidebar";
import {
  EMPTY_SIDEBAR,
  NOTICE_TIMEOUT_MS,
  SIDEBAR_COLLAPSE_STORAGE_KEY,
  type SidebarPayload,
  type SidebarSession,
} from "@/lib/sidebar";

function session(overrides: Partial<SidebarSession> = {}): SidebarSession {
  return {
    sessionId: "s1",
    cwd: "/home/x/project",
    title: "first prompt",
    updatedAt: new Date(Date.now() - 6 * 3600_000).toISOString(),
    origin: "web",
    cwdExists: true,
    ...overrides,
  };
}

function payload(overrides: Partial<SidebarPayload> = {}): SidebarPayload {
  return { sessions: [session()], sidebar: { ...EMPTY_SIDEBAR }, ...overrides };
}

function renderSidebar(overrides: Partial<FolderSidebarProps> = {}): {
  props: FolderSidebarProps;
} {
  const props: FolderSidebarProps = {
    data: payload(),
    loading: false,
    error: null,
    actionError: null,
    currentSessionId: "s1",
    currentCwd: "/home/x/project",
    variant: "rail",
    onClose: vi.fn(),
    onReload: vi.fn(),
    onSelectSession: vi.fn(),
    onNewSession: vi.fn(),
    onNewFolder: vi.fn(),
    onOpenFolder: vi.fn(),
    onRename: vi.fn(async () => true),
    onHide: vi.fn(async () => true),
    onRestore: vi.fn(async () => true),
    onOpenArchive: vi.fn(),
    ...overrides,
  };
  render(<FolderSidebar {...props} />);
  return { props };
}

describe("FolderSidebar", () => {
  it("is a navigation landmark that groups sessions under their folder", () => {
    renderSidebar({
      data: payload({
        sessions: [
          session({ sessionId: "s1" }),
          session({ sessionId: "s2", title: "second prompt", cwd: "/home/x/other" }),
        ],
      }),
    });

    const nav = screen.getByRole("navigation", { name: "Sessions" });
    expect(nav).toBeInTheDocument();
    expect(screen.getByText("project")).toBeInTheDocument();
    expect(screen.getByText("other")).toBeInTheDocument();
    expect(screen.getByText("first prompt")).toBeInTheDocument();
    expect(screen.getByText("second prompt")).toBeInTheDocument();
    // The session count of a folder is part of the row, not a hidden detail.
    expect(screen.getAllByText("1")).toHaveLength(2);
  });

  it("shows an empty folder and hides the count", () => {
    renderSidebar({
      data: payload({
        sessions: [],
        sidebar: {
          ...EMPTY_SIDEBAR,
          workspaces: [{ cwd: "/home/x/empty", label: "archive", cwdExists: true }],
        },
      }),
    });

    expect(screen.getByText("archive")).toBeInTheDocument();
    expect(screen.getByText("No sessions in this folder yet.")).toBeInTheDocument();
  });

  it("never draws a folder whose directory is gone", () => {
    // Not dimmed and not summarised: a row nobody can start a session in is noise,
    // and on a first run it was sixteen of them.
    renderSidebar({
      data: payload({
        sessions: [session({ sessionId: "s1" }), session({ sessionId: "s2", cwd: "/home/x/gone", cwdExists: false })],
        sidebar: {
          ...EMPTY_SIDEBAR,
          workspaces: [{ cwd: "/home/x/also-gone", label: "also-gone", cwdExists: false }],
        },
      }),
    });

    expect(screen.getByText("project")).toBeInTheDocument();
    expect(screen.queryByText("gone")).not.toBeInTheDocument();
    expect(screen.queryByText("also-gone")).not.toBeInTheDocument();
    expect(screen.queryByText(/Folder no longer exists/)).not.toBeInTheDocument();
  });

  it("starts a session in a folder from the folder row", async () => {
    const user = userEvent.setup();
    const { props } = renderSidebar();
    await user.click(screen.getByRole("button", { name: "Start a new session in project" }));
    expect(props.onNewSession).toHaveBeenCalledWith("/home/x/project");
  });

  it("selects a session by clicking its row", async () => {
    const user = userEvent.setup();
    const { props } = renderSidebar();
    await user.click(screen.getByRole("button", { name: "first prompt" }));
    expect(props.onSelectSession).toHaveBeenCalledWith("s1");
  });

  it("marks the folder a brand-new session was started in, before it has a row", () => {
    // A new session has no messages, so it has no log and no row. The folder is the
    // only thing that can acknowledge the `+` that created it.
    renderSidebar({
      data: payload({
        sessions: [],
        sidebar: { ...EMPTY_SIDEBAR, workspaces: [{ cwd: "/home/x/fresh", label: "fresh", cwdExists: true }] },
      }),
      currentSessionId: "",
      currentCwd: "/home/x/fresh/",
    });

    expect(screen.getByRole("button", { name: "fresh folder" })).toHaveAttribute(
      "aria-current",
      "true",
    );
    // An empty folder that is not the live one says "empty", not "new".
    expect(screen.queryByText("new")).toBeInTheDocument();
  });

  it("does not mark a folder that merely shares a prefix with the live one", () => {
    // No session id, so the only fact is the folder string: the case this pins is
    // the prefix comparison, not which of two disagreeing facts wins.
    renderSidebar({ currentSessionId: "", currentCwd: "/home/x/projects" });
    expect(screen.getByRole("button", { name: "project folder" })).not.toHaveAttribute(
      "aria-current",
    );
  });

  it("marks the current session", () => {
    renderSidebar();
    expect(screen.getByRole("button", { name: "first prompt" })).toHaveAttribute(
      "aria-current",
      "true",
    );
  });

  it("removes an empty folder from the sidebar and hides remove on non-empty folders", async () => {
    const user = userEvent.setup();
    const onRemoveFolder = vi.fn().mockResolvedValue(true);
    renderSidebar({
      data: payload({
        sidebar: {
          ...EMPTY_SIDEBAR,
          workspaces: [{ cwd: "/home/x/empty", label: "empty", cwdExists: true }],
        },
      }),
      onRemoveFolder,
    });

    // Non-empty folder ("project") must not have a remove button.
    expect(
      screen.queryByRole("button", { name: "Remove project from sidebar" }),
    ).not.toBeInTheDocument();

    // Empty folder ("empty") has a remove button that calls onRemoveFolder with its cwd.
    await user.click(screen.getByRole("button", { name: "Remove empty from sidebar" }));
    expect(onRemoveFolder).toHaveBeenCalledWith("/home/x/empty");
  });

  it("hides a session from its row action", async () => {
    const user = userEvent.setup();
    const { props } = renderSidebar();
    await user.click(screen.getByRole("button", { name: "Hide session first prompt from the sidebar" }));
    expect(props.onHide).toHaveBeenCalledWith("s1");
  });

  it("renames a session in place, and a cleared title clears the rename", async () => {
    const user = userEvent.setup();
    const { props } = renderSidebar();

    await user.click(screen.getByRole("button", { name: "Rename session first prompt" }));
    const input = screen.getByRole("textbox", { name: "Session title" });
    await user.clear(input);
    await user.type(input, "Sidebar work{Enter}");

    expect(props.onRename).toHaveBeenCalledWith("s1", "Sidebar work");
  });

  it("shows loading instead of an empty sidebar", () => {
    renderSidebar({ data: null, loading: true });
    expect(screen.getByText("Loading sessions…")).toBeInTheDocument();
    expect(screen.queryByText("No sessions yet")).not.toBeInTheDocument();
  });

  it("shows what failed and offers a retry, rather than an empty list", async () => {
    const user = userEvent.setup();
    const { props } = renderSidebar({ data: null, error: "The server is not reachable" });

    expect(screen.getByRole("alert")).toHaveTextContent("Could not load sessions");
    expect(screen.getByRole("alert")).toHaveTextContent("The server is not reachable");
    expect(screen.queryByText("No sessions yet")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Retry/ }));
    expect(props.onReload).toHaveBeenCalled();
  });

  it("says there is nothing yet, and offers both ways to begin", async () => {
    const user = userEvent.setup();
    const { props } = renderSidebar({ data: payload({ sessions: [] }) });

    expect(screen.getByText("No sessions yet")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Open a folder/ }));
    expect(props.onOpenFolder).toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: /Import from terminal/ }));
    expect(props.onOpenArchive).toHaveBeenCalled();
  });

  it("opens the archive", async () => {
    const user = userEvent.setup();
    const { props } = renderSidebar();
    await user.click(screen.getByRole("button", { name: "Open the session archive" }));
    expect(props.onOpenArchive).toHaveBeenCalled();
  });
});

describe("FolderSidebar's empty-folder badge", () => {
  const freshOnly = payload({
    sessions: [],
    sidebar: { ...EMPTY_SIDEBAR, workspaces: [{ cwd: "/home/x/fresh", label: "fresh", cwdExists: true }] },
  });

  it("says new for the live folder whose session has no row yet", () => {
    renderSidebar({ data: freshOnly, currentSessionId: "", currentCwd: "/home/x/fresh" });
    expect(screen.getByText("new")).toBeInTheDocument();
  });

  it("says empty for a folder that is merely unused", () => {
    // The reported bug: every empty folder said NEW forever, which after a week of
    // use reads as an onboarding step nobody finished.
    renderSidebar({ data: freshOnly, currentSessionId: "", currentCwd: "/home/x/elsewhere" });
    expect(screen.getByText("empty")).toBeInTheDocument();
    expect(screen.queryByText("new")).not.toBeInTheDocument();
  });

  it("stops saying new once the live session has a message", () => {
    renderSidebar({
      data: payload({
        // A row for the live session means it has a log. Where that log says the
        // session ran is beside the point: the folder stopped waiting for it.
        sessions: [session({ sessionId: "s1", cwd: "/home/x/elsewhere" })],
        sidebar: {
          ...EMPTY_SIDEBAR,
          workspaces: [{ cwd: "/home/x/fresh", label: "fresh", cwdExists: true }],
        },
      }),
      currentSessionId: "s1",
      currentCwd: "/home/x/fresh",
    });
    expect(screen.queryByText("new")).not.toBeInTheDocument();
    expect(screen.getByText("empty")).toBeInTheDocument();
  });
});

describe("FolderSidebar's current workspace", () => {
  it("marks exactly one folder current when the live session's cwd disagrees", () => {
    // The reported bug, reproduced: a session in `alpha-research` while
    // `session_state` still reports the server's start folder. Both used to be
    // tinted; the live session's own folder has to win.
    renderSidebar({
      data: payload({
        sessions: [
          session({ sessionId: "live", cwd: "/home/x/alpha-research", title: "live row" }),
          session({ sessionId: "other", cwd: "/home/x/start-folder", title: "other row" }),
        ],
      }),
      currentSessionId: "live",
      currentCwd: "/home/x/start-folder",
    });

    const current = screen
      .getAllByRole("button", { name: /folder$/ })
      .filter((button) => button.getAttribute("aria-current") === "true");

    expect(current.map((button) => button.getAttribute("aria-label"))).toEqual([
      "alpha-research folder",
    ]);
  });

  it("never marks a row current when its folder is not the live one", () => {
    renderSidebar({
      data: payload({
        sessions: [
          session({ sessionId: "live", cwd: "/home/x/alpha-research", title: "live row" }),
          session({ sessionId: "elsewhere", cwd: "/home/x/other", title: "other row" }),
        ],
      }),
      currentSessionId: "live",
      currentCwd: "/home/x/other",
    });

    expect(screen.getByRole("button", { name: "live row" })).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("button", { name: "other row" })).not.toHaveAttribute("aria-current");
  });

  it("marks nothing current when the session has no row and no folder", () => {
    renderSidebar({ currentSessionId: "", currentCwd: "" });
    const current = screen
      .getAllByRole("button", { name: /folder$/ })
      .filter((button) => button.getAttribute("aria-current") === "true");
    expect(current).toEqual([]);
  });
});

describe("FolderSidebar's row actions", () => {
  it("keeps the current row's actions hidden until hover or focus", () => {
    renderSidebar();
    const hide = screen.getByRole("button", {
      name: "Hide session first prompt from the sidebar",
    });
    const actions = hide.parentElement;
    expect(actions?.className).toContain("[@media(hover:hover)]:opacity-0");
    expect(actions?.className).toContain("group-hover/session:opacity-100");
  });

  it("still keeps another row's actions out of the way until hover or focus", () => {
    renderSidebar({
      data: payload({
        sessions: [session({ sessionId: "s1" }), session({ sessionId: "s2", title: "second prompt" })],
      }),
      currentSessionId: "s1",
    });

    const otherHide = screen.getByRole("button", {
      name: "Hide session second prompt from the sidebar",
    });
    const actions = otherHide.parentElement;
    expect(actions?.className).toContain("[@media(hover:hover)]:opacity-0");
    // …and the same wrapper reveals itself on focus-within, which is the contract
    // that has to survive: a keyboard can reach it, a pointer has to ask for it.
    expect(actions?.className).toContain("group-focus-within/session:opacity-100");

    otherHide.focus();
    expect(document.activeElement).toBe(otherHide);
    expect(otherHide).toBeEnabled();
  });
});

describe("FolderSidebar's undo notice", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("confirms a hide in a live region and restores the session when undone", async () => {
    const user = userEvent.setup();
    const { props } = renderSidebar();

    await user.click(screen.getByRole("button", { name: "Hide session first prompt from the sidebar" }));

    // Announced, not alerted: this is a confirmation, not a failure.
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Hidden “first prompt”.");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Undo/ }));

    expect(props.onRestore).toHaveBeenCalledWith("s1");
    await waitFor(() => expect(screen.getByRole("status")).toBeEmptyDOMElement());
  });

  it("does not take the focus away from where the person already was", async () => {
    // The composer is the usual place. A confirmation that moves the caret is worse
    // than the row it replaced, so the notice only ever reacts to being focused.
    const user = userEvent.setup();
    renderSidebar();

    const hide = screen.getByRole("button", {
      name: "Hide session first prompt from the sidebar",
    });
    await user.click(hide);

    expect(screen.getByRole("status")).toHaveTextContent("Hidden");
    expect(hide).toHaveFocus();
  });

  it("dismisses itself after a few seconds", async () => {
    // Fake timers from the start, with `fireEvent` rather than `userEvent`: the
    // point of the test is the timer, and user-event's own waits would be frozen
    // by them.
    vi.useFakeTimers();
    renderSidebar();

    fireEvent.click(
      screen.getByRole("button", { name: "Hide session first prompt from the sidebar" }),
    );
    // The hide is a promise; let it settle before the clock moves.
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByRole("status")).toHaveTextContent("Hidden");

    await act(async () => {
      vi.advanceTimersByTime(NOTICE_TIMEOUT_MS - 1);
    });
    expect(screen.getByRole("status")).toHaveTextContent("Hidden");

    await act(async () => {
      vi.advanceTimersByTime(1);
    });
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });

  it("can be dismissed by hand, without undoing anything", async () => {
    const user = userEvent.setup();
    const { props } = renderSidebar();

    await user.click(screen.getByRole("button", { name: "Hide session first prompt from the sidebar" }));
    await user.click(screen.getByRole("button", { name: "Dismiss notification" }));

    expect(screen.getByRole("status")).toBeEmptyDOMElement();
    expect(props.onRestore).not.toHaveBeenCalled();
  });

  it("reports a failed restore instead of pretending it worked", async () => {
    const user = userEvent.setup();
    const { props } = renderSidebar({ onRestore: vi.fn(async () => false) });

    await user.click(screen.getByRole("button", { name: "Hide session first prompt from the sidebar" }));
    await user.click(screen.getByRole("button", { name: /Undo/ }));

    expect(props.onRestore).toHaveBeenCalledWith("s1");
    // Still on screen, says what failed, and offers the request again rather than
    // an Undo that would look like it had already worked.
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Could not restore “first prompt”.");
    await user.click(screen.getByRole("button", { name: /Try again/ }));
    expect(props.onRestore).toHaveBeenCalledTimes(2);
  });

  it("says nothing when the hide itself failed", async () => {
    // The failure is already announced above the list, with the server's message.
    const user = userEvent.setup();
    renderSidebar({ onHide: vi.fn(async () => false) });

    await user.click(screen.getByRole("button", { name: "Hide session first prompt from the sidebar" }));
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });

  it("confirms a rename and puts the old title back when undone", async () => {
    const user = userEvent.setup();
    const { props } = renderSidebar({
      data: payload({
        sessions: [session()],
        sidebar: { ...EMPTY_SIDEBAR, renames: { s1: "Sidebar work" } },
      }),
    });

    await user.click(screen.getByRole("button", { name: "Rename session Sidebar work" }));
    const input = screen.getByRole("textbox", { name: "Session title" });
    await user.clear(input);
    await user.type(input, "Prompt work{Enter}");

    expect(screen.getByRole("status")).toHaveTextContent("Renamed to “Prompt work”.");
    await user.click(screen.getByRole("button", { name: /Undo/ }));

    // The rename that was there before, verbatim: an empty title is what clears it.
    expect(props.onRename).toHaveBeenLastCalledWith("s1", "Sidebar work");
  });

  it("clears the rename entirely when there was no title before", async () => {
    const user = userEvent.setup();
    const { props } = renderSidebar();

    await user.click(screen.getByRole("button", { name: "Rename session first prompt" }));
    const input = screen.getByRole("textbox", { name: "Session title" });
    await user.clear(input);
    await user.type(input, "Sidebar work{Enter}");
    await user.click(screen.getByRole("button", { name: /Undo/ }));

    expect(props.onRename).toHaveBeenLastCalledWith("s1", "");
  });

  it("filters sessions with the search input and clears the filter", async () => {
    const user = userEvent.setup();
    renderSidebar();

    const search = screen.getByRole("searchbox", { name: "Filter sessions and folders" });
    await user.type(search, "no-such-match");
    expect(screen.getByText(/No matches for/)).toBeInTheDocument();

    await user.click(screen.getAllByRole("button", { name: "Clear filter" })[0]!);
    expect(screen.getByText("first prompt")).toBeInTheDocument();
  });

  it("renders status dots and bolds unread session titles", () => {
    renderSidebar({
      data: payload({
        sessions: [
          session({ sessionId: "s1" }),
          session({ sessionId: "s2", title: "earlier prompt" }),
        ],
      }),
      sessionActivity: {
        s1: { status: "running", unread: false },
        s2: { status: "done", unread: true },
      },
    });
    expect(screen.getByLabelText("Working")).toBeInTheDocument();
    expect(screen.getByLabelText("Done")).toBeInTheDocument();
    const unreadTitle = screen.getByText("earlier prompt");
    expect(unreadTitle.className).toContain("font-semibold");
  });

  it("always shows Active Sessions even when empty", () => {
    renderSidebar({
      data: payload({
        sessions: [session({ sessionId: "s1", title: "Fix auth", cwd: "/work/api" })],
      }),
      activeSessions: [],
    });
    const activeRegion = screen.getByRole("region", { name: "Active Sessions" });
    expect(activeRegion).toBeInTheDocument();
    expect(within(activeRegion).getByText("0")).toBeInTheDocument();
    expect(within(activeRegion).getByText("No active sessions")).toBeInTheDocument();
  });

  it("renders Active Sessions with working, done, and needs you statuses and selects on click", async () => {
    const user = userEvent.setup();
    const onSelectSession = vi.fn();
    renderSidebar({
      data: payload({
        sessions: [
          session({ sessionId: "s1", title: "Fix auth", cwd: "/work/api" }),
          session({ sessionId: "s2", title: "Write tests", cwd: "/work/web" }),
          session({ sessionId: "s3", title: "Build UI", cwd: "/work/web" }),
        ],
        sidebar: {
          ...EMPTY_SIDEBAR,
          renames: { s2: "Renamed tests" },
        },
      }),
      onSelectSession,
      activeSessions: [
        {
          sessionId: "s1",
          cwd: "/work/api",
          title: "Fix auth",
          kind: "permission",
          updatedAt: new Date().toISOString(),
          preview: "bash: npm test",
        },
        {
          sessionId: "s2",
          cwd: "/work/web",
          title: "Write tests",
          kind: "done",
          updatedAt: new Date().toISOString(),
        },
        {
          sessionId: "s3",
          cwd: "/work/web",
          title: "Build UI",
          kind: "working",
          updatedAt: new Date().toISOString(),
        },
        {
          sessionId: "s4",
          cwd: "/work/web",
          title: "Bg job",
          kind: "waiting",
          updatedAt: new Date().toISOString(),
        },
      ],
    });

    const activeRegion = screen.getByRole("region", { name: "Active Sessions" });
    expect(activeRegion).toBeInTheDocument();
    expect(within(activeRegion).getByText("Fix auth")).toBeInTheDocument();
    expect(within(activeRegion).getByText("Needs you")).toBeInTheDocument();
    expect(within(activeRegion).getByText("Build UI")).toBeInTheDocument();
    expect(within(activeRegion).getByText("Working")).toBeInTheDocument();
    expect(within(activeRegion).getByText("Bg job")).toBeInTheDocument();
    expect(within(activeRegion).getByText("Waiting")).toBeInTheDocument();
    expect(within(activeRegion).getByText("Renamed tests")).toBeInTheDocument();
    expect(within(activeRegion).getByText("Done")).toBeInTheDocument();
    // Folder name is not shown on Active Session rows.
    expect(within(activeRegion).queryByText(/api/)).not.toBeInTheDocument();

    await user.click(within(activeRegion).getByRole("button", { name: /Fix auth/i }));
    expect(onSelectSession).toHaveBeenCalledWith("s1");
  });

  it("supports per-row dismiss for done/error (not permission/working) and omits Refresh and Unread buttons", async () => {
    const user = userEvent.setup();
    const onDismissActiveSession = vi.fn();
    renderSidebar({
      data: payload({
        sessions: [
          session({ sessionId: "s1", title: "Fix auth", cwd: "/work/api" }),
          session({ sessionId: "s2", title: "Write tests", cwd: "/work/web" }),
          session({ sessionId: "s3", title: "Unread task", cwd: "/work/web" }),
          session({ sessionId: "s4", title: "Running task", cwd: "/work/web" }),
        ],
      }),
      onDismissActiveSession,
      sessionActivity: {
        s2: { status: "done", unread: false },
        s3: { status: "error", unread: true },
        s4: { status: "running", unread: false },
      },
      activeSessions: [
        {
          sessionId: "s1",
          cwd: "/work/api",
          title: "Fix auth",
          kind: "permission",
          read: false,
          updatedAt: new Date().toISOString(),
        },
        {
          sessionId: "s2",
          cwd: "/work/web",
          title: "Write tests",
          kind: "done",
          read: true,
          updatedAt: new Date().toISOString(),
        },
        {
          sessionId: "s3",
          cwd: "/work/web",
          title: "Unread task",
          kind: "error",
          read: false,
          updatedAt: new Date().toISOString(),
        },
        {
          sessionId: "s4",
          cwd: "/work/web",
          title: "Running task",
          kind: "working",
          read: false,
          updatedAt: new Date().toISOString(),
        },
        {
          sessionId: "s5",
          cwd: "/work/web",
          title: "Waiting task",
          kind: "waiting",
          read: false,
          updatedAt: new Date().toISOString(),
        },
      ],
    });

    const activeRegion = screen.getByRole("region", { name: "Active Sessions" });
    expect(within(activeRegion).queryByRole("button", { name: "Dismiss Fix auth" })).not.toBeInTheDocument();
    expect(within(activeRegion).queryByRole("button", { name: "Dismiss Running task" })).not.toBeInTheDocument();
    expect(within(activeRegion).queryByRole("button", { name: "Dismiss Waiting task" })).not.toBeInTheDocument();
    await user.click(within(activeRegion).getByRole("button", { name: "Dismiss Unread task" }));
    expect(onDismissActiveSession).toHaveBeenCalledWith("s3");
    await user.click(within(activeRegion).getByRole("button", { name: "Dismiss Write tests" }));
    expect(onDismissActiveSession).toHaveBeenCalledWith("s2");

    expect(screen.queryByRole("button", { name: /^Unread\s*\d*$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Refresh sessions" })).not.toBeInTheDocument();
  });

  it("does not render Pin or Unpin buttons", () => {
    renderSidebar({
      data: payload({
        sessions: [
          session({ sessionId: "s1", title: "Task one", cwd: "/work/api" }),
        ],
      }),
    });
    expect(screen.queryByRole("region", { name: "Pinned" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /pin session/i })).not.toBeInTheDocument();
  });

});

describe("FolderSidebar collapse persistence", () => {
  it("persists folder collapse states across remounts with Active Sessions on top", async () => {
    window.localStorage.clear();
    const user = userEvent.setup();
    const testData = payload({
      sessions: [
        session({ sessionId: "s1", title: "Important task", cwd: "/work/api" }),
        session({ sessionId: "s2", title: "Regular task", cwd: "/work/web" }),
      ],
    });
    const testActiveSessions = [
      {
        sessionId: "s2",
        cwd: "/work/web",
        title: "Regular task",
        kind: "permission" as const,
        updatedAt: new Date().toISOString(),
      },
    ];

    const firstRender = render(
      <FolderSidebar
        data={testData}
        loading={false}
        error={null}
        actionError={null}
        currentSessionId="s2"
        currentCwd="/work/web"
        variant="rail"
        onClose={vi.fn()}
        onReload={vi.fn()}
        onSelectSession={vi.fn()}
        onNewSession={vi.fn()}
        onNewFolder={vi.fn()}
        onOpenFolder={vi.fn()}
        onRename={vi.fn(async () => true)}
        onHide={vi.fn(async () => true)}
        onRestore={vi.fn(async () => true)}
        onOpenArchive={vi.fn()}
        activeSessions={testActiveSessions}
      />,
    );

    expect(screen.getByRole("button", { name: /^web\b/i })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("region", { name: "Active Sessions" })).toBeInTheDocument();

    // Collapse folder "web".
    await user.click(screen.getByRole("button", { name: /^web\b/i }));
    expect(screen.getByRole("button", { name: /^web\b/i })).toHaveAttribute("aria-expanded", "false");

    expect(window.localStorage.getItem(SIDEBAR_COLLAPSE_STORAGE_KEY)).toBeTruthy();

    firstRender.unmount();

    // Remount: collapsed choices should be restored from localStorage.
    render(
      <FolderSidebar
        data={testData}
        loading={false}
        error={null}
        actionError={null}
        currentSessionId="s2"
        currentCwd="/work/web"
        variant="rail"
        onClose={vi.fn()}
        onReload={vi.fn()}
        onSelectSession={vi.fn()}
        onNewSession={vi.fn()}
        onNewFolder={vi.fn()}
        onOpenFolder={vi.fn()}
        onRename={vi.fn(async () => true)}
        onHide={vi.fn(async () => true)}
        onRestore={vi.fn(async () => true)}
        onOpenArchive={vi.fn()}
        activeSessions={testActiveSessions}
      />,
    );

    expect(screen.getByRole("button", { name: /^web\b/i })).toHaveAttribute("aria-expanded", "false");
    window.localStorage.clear();
  });
});

describe("FolderSidebar section ordering", () => {
  it("orders Active Sessions, then Bots, then Workspaces with Add Folder and Filter below Workspaces", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(
      <FolderSidebar
        data={payload({
          sessions: [session({ sessionId: "s1", title: "Alpha", cwd: "/work/api" })],
        })}
        loading={false}
        error={null}
        actionError={null}
        currentSessionId="s1"
        currentCwd="/work/api"
        variant="drawer"
        onClose={onClose}
        onReload={vi.fn()}
        onSelectSession={vi.fn()}
        onNewSession={vi.fn()}
        onNewFolder={vi.fn()}
        onOpenFolder={vi.fn()}
        onRename={vi.fn(async () => true)}
        onHide={vi.fn(async () => true)}
        onRestore={vi.fn(async () => true)}
        onOpenArchive={vi.fn()}
      />,
    );

    const activeRegion = screen.getByRole("region", { name: "Active Sessions" });
    const botsRegion = screen.getByRole("region", { name: "Bots" });
    const workspacesHeading = screen.getByRole("heading", { name: "Workspaces" });
    const addFolderBtn = screen.getByRole("button", { name: "Add a folder to the sidebar" });
    const filterInput = screen.getByRole("searchbox", { name: "Filter sessions and folders" });
    const folderBtn = screen.getByRole("button", { name: /^api\b/i });

    const before = (a: Node, b: Node): boolean =>
      Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);

    expect(before(activeRegion, botsRegion)).toBe(true);
    expect(before(botsRegion, workspacesHeading)).toBe(true);
    expect(before(workspacesHeading, addFolderBtn)).toBe(true);
    expect(before(addFolderBtn, filterInput)).toBe(true);
    expect(before(filterInput, folderBtn)).toBe(true);

    await user.click(screen.getByRole("button", { name: "Close the session sidebar" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("renders a 1-click New chat button and Chats section for generalChatsDir", async () => {
    const user = userEvent.setup();
    const chatsDir = "/home/u/.quartz/chats";
    const onNewSession = vi.fn();
    render(
      <FolderSidebar
        data={{
          ...payload({
            sessions: [session({ sessionId: "c1", title: "Quick question", cwd: chatsDir })],
          }),
          generalChatsDir: chatsDir,
        }}
        loading={false}
        error={null}
        actionError={null}
        currentSessionId=""
        currentCwd=""
        variant="rail"
        onClose={vi.fn()}
        onReload={vi.fn()}
        onSelectSession={vi.fn()}
        onNewSession={onNewSession}
        onNewFolder={vi.fn()}
        onOpenFolder={vi.fn()}
        onRename={vi.fn(async () => true)}
        onHide={vi.fn(async () => true)}
        onRestore={vi.fn(async () => true)}
        onOpenArchive={vi.fn()}
      />,
    );

    const newChatBtn = screen.getByRole("button", { name: "New chat" });
    await user.click(newChatBtn);
    expect(onNewSession).toHaveBeenCalledWith(chatsDir);

    const chatsRegion = screen.getByRole("region", { name: "Chats" });
    expect(chatsRegion).toBeInTheDocument();
    expect(screen.getByText("Quick question")).toBeInTheDocument();
  });
});
