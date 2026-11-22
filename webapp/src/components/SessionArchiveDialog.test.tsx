import { describe, expect, it, vi } from "vitest";
import * as React from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
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

  it("keeps a session whose folder is gone reachable, flagged, and without a no-op action", async () => {
    renderDialog({
      data: {
        sessions: [
          session({ sessionId: "orphan", title: "old worktree", cwdExists: false }),
          session({ sessionId: "visible", title: "already listed", origin: "web" }),
        ],
        sidebar: { ...EMPTY_SIDEBAR },
      },
    });

    expect(await screen.findByText("old worktree")).toBeInTheDocument();
    // Said once, on the folder's heading, not repeated on every row underneath it.
    expect(screen.getByText("folder removed")).toBeInTheDocument();
    // Import would add it to a list that still cannot draw it, so there is no button.
    expect(screen.queryByRole("button", { name: /session old worktree/ })).not.toBeInTheDocument();
    expect(screen.queryByText("already listed")).not.toBeInTheDocument();
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

describe("SessionArchiveDialog's filter", () => {
  /** A realistic archive: several folders, all three reasons, enough rows to scan. */
  function crowded(): SidebarPayload {
    const sessions: SidebarSession[] = [
      // Two hidden web sessions, two terminal sessions, one dead folder: one reason
      // each, so a count in the test means exactly one thing.
      session({ sessionId: "h1", title: "deploy notes", cwd: "/home/x/alpha", origin: "web" }),
      session({ sessionId: "h2", title: "late night fix", cwd: "/home/x/alpha", origin: "web" }),
      session({ sessionId: "t1", title: "pytest run", cwd: "/home/x/beta", origin: "cli" }),
      session({ sessionId: "t2", title: "migration dry run", cwd: "/home/x/beta", origin: "cli" }),
      session({
        sessionId: "o1",
        title: "old worktree work",
        cwd: "/home/x/gone",
        origin: "web",
        cwdExists: false,
      }),
    ];
    return {
      sessions,
      sidebar: { ...EMPTY_SIDEBAR, hiddenSessions: ["h1", "h2"] },
    };
  }

  it("focuses the filter on open, and says how many rows there are", async () => {
    renderDialog({ data: crowded() });

    const filter = await screen.findByRole("textbox", { name: "Filter sessions" });
    await waitFor(() => expect(filter).toHaveFocus());
    // Before scrolling: what am I looking at?
    expect(screen.getByText("5 sessions")).toBeInTheDocument();
  });

  it("matches the title and the folder", async () => {
    const user = userEvent.setup();
    renderDialog({ data: crowded() });
    const filter = await screen.findByRole("textbox", { name: "Filter sessions" });

    await user.type(filter, "migration");
    expect(screen.getByText("migration dry run")).toBeInTheDocument();
    expect(screen.queryByText("deploy notes")).not.toBeInTheDocument();

    await user.clear(filter);
    // A folder name finds every session in it, which is half of how you remember one.
    await user.type(filter, "beta");
    expect(screen.getByText("pytest run")).toBeInTheDocument();
    expect(screen.getByText("migration dry run")).toBeInTheDocument();
    expect(screen.queryByText("deploy notes")).not.toBeInTheDocument();
    expect(screen.getByText("2 of 5")).toBeInTheDocument();
  });

  it("says so rather than going blank when nothing matches", async () => {
    const user = userEvent.setup();
    renderDialog({ data: crowded() });
    const filter = await screen.findByRole("textbox", { name: "Filter sessions" });

    await user.type(filter, "no such session");
    expect(screen.getByText("No session matches \u201cno such session\u201d.")).toBeInTheDocument();
    expect(screen.queryByText("Every session is already in the sidebar.")).not.toBeInTheDocument();
  });

  it("groups rows under their folder, with a count each", async () => {
    renderDialog({ data: crowded() });

    const alpha = await screen.findByRole("region", { name: /alpha/ });
    expect(within(alpha).getAllByRole("listitem")).toHaveLength(2);
    const beta = screen.getByRole("region", { name: /beta/ });
    expect(within(beta).getAllByRole("listitem")).toHaveLength(2);
  });

  it("filters by reason with chips that count, instead of a badge on every row", async () => {
    const user = userEvent.setup();
    renderDialog({ data: crowded() });

    const hidden = await screen.findByRole("button", { name: /^Hidden/ });
    const terminal = screen.getByRole("button", { name: /^Terminal/ });
    const missing = screen.getByRole("button", { name: /^Folder removed/ });

    expect(hidden).toHaveTextContent("2");
    expect(terminal).toHaveTextContent("2");
    expect(missing).toHaveTextContent("1");
    expect(hidden).toHaveAttribute("aria-pressed", "false");

    await user.click(terminal);
    expect(terminal).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("pytest run")).toBeInTheDocument();
    expect(screen.queryByText("deploy notes")).not.toBeInTheDocument();

    await user.click(hidden);
    // Reasons are OR-ed: both views at once, and the dead folder stays out of both.
    expect(screen.getByText("pytest run")).toBeInTheDocument();
    expect(screen.getByText("deploy notes")).toBeInTheDocument();
    expect(screen.queryByText("old worktree work")).not.toBeInTheDocument();
    expect(screen.getByText("4 of 5")).toBeInTheDocument();

    await user.click(terminal);
    await user.click(hidden);
    expect(screen.getByText("5 sessions")).toBeInTheDocument();
  });

  it("offers one way back to the full list, and clears both kinds of filter", async () => {
    const user = userEvent.setup();
    renderDialog({ data: crowded() });

    await user.click(await screen.findByRole("button", { name: /^Folder removed/ }));
    expect(screen.queryByText("Every session is already in the sidebar.")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear" }));

    expect(screen.getByText("5 sessions")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Folder removed/ })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    // The control disappears when there is nothing left to clear.
    expect(screen.queryByRole("button", { name: "Clear" })).not.toBeInTheDocument();
  });

  it("keeps a reason nobody has out of the way", async () => {
    renderDialog({
      data: { sessions: [session({ sessionId: "t1", title: "only terminal" })], sidebar: { ...EMPTY_SIDEBAR } },
    });
    expect(await screen.findByRole("button", { name: /^Hidden/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /^Terminal/ })).toBeEnabled();
  });
});

describe("SessionArchiveDialog's keyboard contract", () => {
  it("closes on Escape", async () => {
    const user = userEvent.setup();
    const { props } = renderDialog();

    await screen.findByRole("dialog");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(props.onOpenChange).toHaveBeenCalledWith(false));
  });

  it("gives focus back to whatever opened it", async () => {
    const user = userEvent.setup();

    function Harness(): React.ReactElement {
      const [open, setOpen] = React.useState(false);
      const opener = React.useRef<HTMLElement | null>(null);
      return (
        <div>
          <button
            type="button"
            onClick={(event) => {
              opener.current = event.currentTarget;
              setOpen(true);
            }}
          >
            Open the session archive
          </button>
          <SessionArchiveDialog
            open={open}
            onOpenChange={setOpen}
            data={{ sessions: [session()], sidebar: { ...EMPTY_SIDEBAR } }}
            loading={false}
            error={null}
            actionError={null}
            onReload={vi.fn()}
            onImport={vi.fn(async () => true)}
            onRestore={vi.fn(async () => true)}
            returnFocusTo={opener}
          />
        </div>
      );
    }

    render(<Harness />);
    const opener = screen.getByRole("button", { name: "Open the session archive" });
    await user.click(opener);
    await screen.findByRole("dialog");

    await user.keyboard("{Escape}");

    // Otherwise the next Tab starts at the top of the page.
    await waitFor(() => expect(opener).toHaveFocus());
  });
});
