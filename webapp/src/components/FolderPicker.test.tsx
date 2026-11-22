import * as React from "react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FolderPicker } from "./FolderPicker";
import type { DirectoryListing } from "@/lib/folderPicker";

const listDirectories = vi.fn<(path?: string) => Promise<DirectoryListing>>();
const createDirectory = vi.fn<(path: string, name: string) => Promise<DirectoryListing>>();

vi.mock("@/lib/sidebarApi", () => ({
  listDirectories: (path?: string) => listDirectories(path),
  createDirectory: (path: string, name: string) => createDirectory(path, name),
}));

function listing(overrides: Partial<DirectoryListing> = {}): DirectoryListing {
  return {
    path: "/home/x/project",
    parents: [
      { name: "/", path: "/" },
      { name: "home", path: "/home" },
      { name: "x", path: "/home/x" },
      { name: "project", path: "/home/x/project" },
    ],
    entries: [
      { name: "alpha", path: "/home/x/project/alpha", isDir: true },
      { name: "beta-project", path: "/home/x/project/beta-project", isDir: true },
    ],
    error: null,
    ...overrides,
  };
}

function Harness({
  onChoose = vi.fn(),
  startOpen = true,
}: {
  onChoose?: (path: string) => void;
  startOpen?: boolean;
}): React.ReactElement {
  const [open, setOpen] = React.useState(startOpen);
  return (
    <div>
      <button type="button" onClick={() => setOpen(true)}>
        Open picker
      </button>
      <FolderPicker
        open={open}
        onOpenChange={setOpen}
        onChoose={onChoose}
        title="New session"
        chooseLabel="Start session"
      />
    </div>
  );
}

/** The filter is a combobox: it keeps focus and owns the listbox underneath it. */
function filterField(): HTMLElement {
  return screen.getByRole("combobox", { name: "Filter folders" });
}

describe("FolderPicker", () => {
  beforeEach(() => {
    listDirectories.mockReset();
    createDirectory.mockReset();
    listDirectories.mockResolvedValue(listing());
  });

  it("lists the directories of the current folder as tappable rows", async () => {
    render(<Harness />);

    expect(await screen.findByRole("option", { name: /alpha/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /beta-project/ })).toBeInTheDocument();
    // No path means "start where the server starts": `quartz web --cwd`.
    expect(listDirectories).toHaveBeenCalledWith(undefined);
  });

  it("has a filter that narrows only the current listing", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await screen.findByRole("option", { name: /alpha/ });

    await user.type(filterField(), "beta");

    expect(screen.queryByRole("option", { name: /alpha/ })).not.toBeInTheDocument();
    expect(screen.getByRole("option", { name: /beta-project/ })).toBeInTheDocument();
    // Filtering is not navigation: the directory on screen has not changed.
    expect(listDirectories).toHaveBeenCalledTimes(1);
  });

  it("shows a message when nothing matches, without leaving the folder", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await screen.findByRole("option", { name: /alpha/ });

    await user.type(filterField(), "zzz");

    expect(screen.getByText("No folder matches “zzz”.")).toBeInTheDocument();
    expect(listDirectories).toHaveBeenCalledTimes(1);
  });

  it("descends into a folder using the path the server gave it", async () => {
    const user = userEvent.setup();
    listDirectories.mockResolvedValueOnce(listing()).mockResolvedValueOnce(
      listing({ path: "/home/x/project/alpha", entries: [] }),
    );
    render(<Harness />);

    await user.click(await screen.findByRole("option", { name: /alpha/ }));

    await waitFor(() => {
      expect(listDirectories).toHaveBeenLastCalledWith("/home/x/project/alpha");
    });
  });

  it("walks up with the breadcrumb and the up button", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await screen.findByRole("option", { name: /alpha/ });

    await user.click(screen.getByRole("button", { name: "Go to /home/x" }));
    expect(listDirectories).toHaveBeenLastCalledWith("/home/x");

    await user.click(screen.getByRole("button", { name: "Go up one folder" }));
    expect(listDirectories).toHaveBeenLastCalledWith("/home/x");
  });

  /* ------------------------------------------------------------- the keyboard */

  it("moves the highlight with the arrows and keeps focus in the filter", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await screen.findByRole("option", { name: /alpha/ });

    const field = filterField();
    await user.click(field);
    // The first row is highlighted as soon as the list has rows, so Enter right
    // after opening is not a dead key.
    expect(field).toHaveAttribute("aria-activedescendant", "folder-option-0");
    expect(screen.getByRole("option", { name: /alpha/ })).toHaveAttribute("aria-selected", "true");

    await user.keyboard("{ArrowDown}");
    expect(field).toHaveAttribute("aria-activedescendant", "folder-option-1");
    expect(screen.getByRole("option", { name: /beta-project/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    // The highlight is announced from the field: focus never leaves it.
    expect(field).toHaveFocus();

    await user.keyboard("{ArrowUp}");
    expect(field).toHaveAttribute("aria-activedescendant", "folder-option-0");
    expect(field).toHaveFocus();
  });

  it("wraps the highlight at the ends instead of dying there", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await screen.findByRole("option", { name: /alpha/ });

    const field = filterField();
    await user.click(field);
    await user.keyboard("{ArrowUp}");
    expect(field).toHaveAttribute("aria-activedescendant", "folder-option-1");
  });

  it("opens the highlighted row on Enter", async () => {
    const user = userEvent.setup();
    listDirectories.mockResolvedValueOnce(listing()).mockResolvedValueOnce(
      listing({ path: "/home/x/project/beta-project", entries: [] }),
    );
    render(<Harness />);
    await screen.findByRole("option", { name: /alpha/ });

    await user.click(filterField());
    await user.keyboard("{ArrowDown}{Enter}");

    await waitFor(() => {
      expect(listDirectories).toHaveBeenLastCalledWith("/home/x/project/beta-project");
    });
  });

  it("opens the only match after typing its name into the filter", async () => {
    const user = userEvent.setup();
    listDirectories.mockResolvedValueOnce(listing()).mockResolvedValueOnce(
      listing({ path: "/home/x/project/beta-project", entries: [] }),
    );
    render(<Harness />);
    await screen.findByRole("option", { name: /alpha/ });

    await user.type(filterField(), "beta{Enter}");

    await waitFor(() => {
      expect(listDirectories).toHaveBeenLastCalledWith("/home/x/project/beta-project");
    });
  });

  it("goes up one folder on Backspace when the filter is empty", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await screen.findByRole("option", { name: /alpha/ });

    await user.click(filterField());
    await user.keyboard("{Backspace}");

    expect(listDirectories).toHaveBeenLastCalledWith("/home/x");
  });

  it("uses Backspace to edit the filter while it has text", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await screen.findByRole("option", { name: /alpha/ });

    await user.type(filterField(), "beta{Backspace}");

    expect(filterField()).toHaveValue("bet");
    // A Backspace that deleted something is not a request to leave the folder.
    expect(listDirectories).toHaveBeenCalledTimes(1);
  });

  it("goes up one folder on Alt+ArrowUp, whatever the filter holds", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await screen.findByRole("option", { name: /alpha/ });

    await user.click(filterField());
    await user.keyboard("alp{Alt>}{ArrowUp}{/Alt}");

    expect(listDirectories).toHaveBeenLastCalledWith("/home/x");
  });

  it("leaves the filter for the footer on Tab", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await screen.findByRole("option", { name: /alpha/ });

    await user.click(filterField());
    await user.tab();

    // Rows are options, not tab stops: the next Tab is the first footer button.
    expect(screen.getByRole("button", { name: /New folder/ })).toHaveFocus();
  });

  it("closes on Escape and gives focus back to whatever opened it", async () => {
    const user = userEvent.setup();
    render(<Harness startOpen={false} />);

    const opener = screen.getByRole("button", { name: "Open picker" });
    await user.click(opener);
    await screen.findByRole("option", { name: /alpha/ });
    await waitFor(() => expect(filterField()).toHaveFocus());

    await user.keyboard("{Escape}");

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(opener).toHaveFocus();
  });

  /* ----------------------------------------------------------------- the choice */

  it("does not arm the primary action on the folder it happened to open in", async () => {
    render(<Harness />);
    await screen.findByRole("option", { name: /alpha/ });

    // Nothing has been pointed at yet, so "Start session" cannot start anywhere.
    expect(screen.getByRole("button", { name: "Start session" })).toBeDisabled();
    expect(screen.getByText("not chosen yet")).toBeInTheDocument();
    // The folder on screen is still readable, so the question has an answer.
    expect(screen.getByText("/home/x/project")).toBeInTheDocument();
  });

  it("arms the primary action once a folder has been opened, and starts there", async () => {
    const user = userEvent.setup();
    const onChoose = vi.fn();
    listDirectories
      .mockResolvedValueOnce(listing())
      .mockResolvedValueOnce(listing({ path: "/home/x/project/alpha", entries: [] }));
    render(<Harness onChoose={onChoose} />);
    await screen.findByRole("option", { name: /alpha/ });

    await user.click(screen.getByRole("option", { name: /alpha/ }));

    const start = screen.getByRole("button", { name: "Start session" });
    await waitFor(() => expect(start).not.toBeDisabled());
    // The destination is the folder that was opened, spelled in full.
    expect(screen.getByText("/home/x/project/alpha")).toBeInTheDocument();

    await user.click(start);
    expect(onChoose).toHaveBeenCalledWith("/home/x/project/alpha");
  });

  it("chooses the folder on screen when it is used explicitly", async () => {
    const user = userEvent.setup();
    const onChoose = vi.fn();
    render(<Harness onChoose={onChoose} />);
    await screen.findByRole("option", { name: /alpha/ });

    await user.click(screen.getByRole("button", { name: "Use this folder" }));

    expect(screen.getByText("/home/x/project")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Use this folder" })).not.toBeInTheDocument();
    const start = screen.getByRole("button", { name: "Start session" });
    expect(start).not.toBeDisabled();

    await user.click(start);
    expect(onChoose).toHaveBeenCalledWith("/home/x/project");
  });

  it("creates a folder inside the current directory", async () => {
    const user = userEvent.setup();
    createDirectory.mockResolvedValue(
      listing({
        entries: [
          { name: "alpha", path: "/home/x/project/alpha", isDir: true },
          { name: "gamma", path: "/home/x/project/gamma", isDir: true },
        ],
      }),
    );
    render(<Harness />);
    await screen.findByRole("option", { name: /alpha/ });

    await user.click(screen.getByRole("button", { name: /New folder/ }));
    await user.type(screen.getByRole("textbox", { name: "New folder name" }), "gamma");
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(createDirectory).toHaveBeenCalledWith("/home/x/project", "gamma"));
    expect(await screen.findByRole("option", { name: /gamma/ })).toBeInTheDocument();
  });

  it("makes a freshly created folder the destination rather than just a row", async () => {
    const user = userEvent.setup();
    const onChoose = vi.fn();
    createDirectory.mockResolvedValue(
      listing({
        entries: [
          { name: "alpha", path: "/home/x/project/alpha", isDir: true },
          { name: "gamma", path: "/home/x/project/gamma", isDir: true },
        ],
      }),
    );
    render(<Harness onChoose={onChoose} />);
    await screen.findByRole("option", { name: /alpha/ });

    await user.click(screen.getByRole("button", { name: /New folder/ }));
    await user.type(screen.getByRole("textbox", { name: "New folder name" }), "gamma");
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(screen.getByText("/home/x/project/gamma")).toBeInTheDocument(),
    );
    const start = screen.getByRole("button", { name: "Start session" });
    expect(start).not.toBeDisabled();

    await user.click(start);
    // Straight into the new folder: the listing on screen never had to move.
    expect(onChoose).toHaveBeenCalledWith("/home/x/project/gamma");
    expect(listDirectories).toHaveBeenCalledTimes(1);
  });

  it("shows a rejected mkdir as an error and stays put", async () => {
    const user = userEvent.setup();
    createDirectory.mockResolvedValue(
      listing({ error: "already exists: alpha" }),
    );
    render(<Harness />);
    await screen.findByRole("option", { name: /alpha/ });

    await user.click(screen.getByRole("button", { name: /New folder/ }));
    await user.type(screen.getByRole("textbox", { name: "New folder name" }), "alpha");
    await user.click(screen.getByRole("button", { name: "Create" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("already exists: alpha");
    expect(screen.getByRole("option", { name: /beta-project/ })).toBeInTheDocument();
  });

  it("shows a failed listing with a retry", async () => {
    const user = userEvent.setup();
    listDirectories.mockRejectedValueOnce(new Error("The server is not reachable"));
    listDirectories.mockResolvedValue(listing());
    render(<Harness />);

    expect(await screen.findByText("The server is not reachable")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Retry/ }));

    expect(await screen.findByRole("option", { name: /alpha/ })).toBeInTheDocument();
  });

  it("reports a bad path rather than rendering an empty folder", async () => {
    listDirectories.mockResolvedValue(
      listing({ entries: [], error: "not a directory: /home/x/project" }),
    );
    render(<Harness />);

    expect(await screen.findByText("not a directory: /home/x/project")).toBeInTheDocument();
    // Choosing a directory we could not read would be a lie, so the action is off.
    expect(screen.getByRole("button", { name: "Start session" })).toBeDisabled();
  });
});
