import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SessionArchiveDialog, type SessionArchiveDialogProps } from "./SessionArchiveDialog";
import { EMPTY_SIDEBAR, type SidebarPayload, type SidebarSession } from "@/lib/sidebar";

function session(overrides: Partial<SidebarSession> = {}): SidebarSession {
  return {
    sessionId: "s1",
    cwd: "/home/x/project",
    title: "first prompt",
    updatedAt: new Date().toISOString(),
    origin: "cli",
    cwdExists: true,
    ...overrides,
  };
}

function renderDialog(overrides: Partial<SessionArchiveDialogProps> = {}): {
  props: SessionArchiveDialogProps;
} {
  const props: SessionArchiveDialogProps = {
    open: true,
    onOpenChange: vi.fn(),
    data: {
      sessions: [
        session({ sessionId: "hidden-one", title: "archived prompt", origin: "web" }),
        session({ sessionId: "terminal-one", title: "terminal prompt" }),
      ],
      sidebar: { ...EMPTY_SIDEBAR, hiddenSessions: ["hidden-one"] },
    } satisfies SidebarPayload,
    loading: false,
    error: null,
    actionError: null,
    onReload: vi.fn(),
    onImport: vi.fn(async () => true),
    onRestore: vi.fn(async () => true),
    ...overrides,
  };
  render(<SessionArchiveDialog {...props} />);
  return { props };
}

describe("SessionArchiveDialog", () => {
  it("lists only what the sidebar is not showing", async () => {
    renderDialog({
      data: {
        sessions: [
          session({ sessionId: "visible", title: "already listed", origin: "web" }),
          session({ sessionId: "terminal-one", title: "terminal prompt" }),
        ],
        sidebar: { ...EMPTY_SIDEBAR },
      },
    });

    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveAccessibleName("Not in sidebar");
    expect(screen.getByText("terminal prompt")).toBeInTheDocument();
    expect(screen.queryByText("already listed")).not.toBeInTheDocument();
  });

  it("imports a terminal session", async () => {
    const user = userEvent.setup();
    const { props } = renderDialog();

    await user.click(await screen.findByRole("button", { name: "Import session terminal prompt" }));

    expect(props.onImport).toHaveBeenCalledWith("terminal-one");
    expect(props.onRestore).not.toHaveBeenCalled();
  });

  it("restores a hidden session", async () => {
    const user = userEvent.setup();
    const { props } = renderDialog();

    await user.click(await screen.findByRole("button", { name: "Restore session archived prompt" }));

    expect(props.onRestore).toHaveBeenCalledWith("hidden-one");
    // It came from the web, so a restore alone is enough to list it again.
    expect(props.onImport).not.toHaveBeenCalled();
  });

  it("does both when a terminal session was archived", async () => {
    const user = userEvent.setup();
    const { props } = renderDialog({
      data: {
        sessions: [session({ sessionId: "both", title: "both" })],
        sidebar: { ...EMPTY_SIDEBAR, hiddenSessions: ["both"] },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Restore session both" }));

    expect(props.onImport).toHaveBeenCalledWith("both");
    expect(props.onRestore).toHaveBeenCalledWith("both");
  });

  it("says when there is nothing to bring back", async () => {
    renderDialog({ data: { sessions: [], sidebar: { ...EMPTY_SIDEBAR } } });
    expect(await screen.findByText("Every session is already in the sidebar.")).toBeInTheDocument();
  });

  it("shows a load failure with a retry instead of an empty list", async () => {
    const user = userEvent.setup();
    const { props } = renderDialog({ data: null, error: "The server is not reachable" });

    expect(await screen.findByRole("alert")).toHaveTextContent("The server is not reachable");
    expect(screen.queryByText("Every session is already in the sidebar.")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Retry/ }));
    expect(props.onReload).toHaveBeenCalled();
  });

  it("shows a failed action", async () => {
    renderDialog({ actionError: "Import failed" });
    expect(await screen.findByRole("alert")).toHaveTextContent("Import failed");
  });
});
