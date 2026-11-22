import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FolderSidebar, type FolderSidebarProps } from "./FolderSidebar";
import { EMPTY_SIDEBAR, type SidebarPayload, type SidebarSession } from "@/lib/sidebar";

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
    variant: "rail",
    onClose: vi.fn(),
    onReload: vi.fn(),
    onSelectSession: vi.fn(),
    onNewSession: vi.fn(),
    onNewFolder: vi.fn(),
    onOpenFolder: vi.fn(),
    onRename: vi.fn(),
    onHide: vi.fn(),
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

  it("marks the current session", () => {
    renderSidebar();
    expect(screen.getByRole("button", { name: "first prompt" })).toHaveAttribute(
      "aria-current",
      "true",
    );
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
