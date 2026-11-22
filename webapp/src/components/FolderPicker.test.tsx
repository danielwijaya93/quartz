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
}: {
  onChoose?: (path: string) => void;
}): React.ReactElement {
  const [open, setOpen] = React.useState(true);
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

describe("FolderPicker", () => {
  beforeEach(() => {
    listDirectories.mockReset();
    createDirectory.mockReset();
    listDirectories.mockResolvedValue(listing());
  });

  it("lists the directories of the current folder as tappable rows", async () => {
    render(<Harness />);

    expect(await screen.findByRole("button", { name: /alpha/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /beta-project/ })).toBeInTheDocument();
    // No path means "start where the server starts": `quartz web --cwd`.
    expect(listDirectories).toHaveBeenCalledWith(undefined);
  });

  it("has a filter that narrows only the current listing", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await screen.findByRole("button", { name: /alpha/ });

    const filter = screen.getByRole("textbox", { name: "Filter folders" });
    await user.type(filter, "beta");

    expect(screen.queryByRole("button", { name: /^(?!.*beta).*alpha/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /beta-project/ })).toBeInTheDocument();
    // Filtering is not navigation: the directory on screen has not changed.
    expect(listDirectories).toHaveBeenCalledTimes(1);
  });

  it("shows a message when nothing matches, without leaving the folder", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await screen.findByRole("button", { name: /alpha/ });

    await user.type(screen.getByRole("textbox", { name: "Filter folders" }), "zzz");

    expect(screen.getByText("No folder matches “zzz”.")).toBeInTheDocument();
    expect(listDirectories).toHaveBeenCalledTimes(1);
  });

  it("descends into a folder using the path the server gave it", async () => {
    const user = userEvent.setup();
    listDirectories.mockResolvedValueOnce(listing()).mockResolvedValueOnce(
      listing({ path: "/home/x/project/alpha", entries: [] }),
    );
    render(<Harness />);

    await user.click(await screen.findByRole("button", { name: /alpha/ }));

    await waitFor(() => {
      expect(listDirectories).toHaveBeenLastCalledWith("/home/x/project/alpha");
    });
  });

  it("walks up with the breadcrumb and the up button", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await screen.findByRole("button", { name: /alpha/ });

    await user.click(screen.getByRole("button", { name: "Go to /home/x" }));
    expect(listDirectories).toHaveBeenLastCalledWith("/home/x");

    await user.click(screen.getByRole("button", { name: "Go up one folder" }));
    expect(listDirectories).toHaveBeenLastCalledWith("/home/x");
  });

  it("chooses the folder on screen", async () => {
    const user = userEvent.setup();
    const onChoose = vi.fn();
    render(<Harness onChoose={onChoose} />);
    await screen.findByRole("button", { name: /alpha/ });

    await user.click(screen.getByRole("button", { name: "Start session" }));

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
    await screen.findByRole("button", { name: /alpha/ });

    await user.click(screen.getByRole("button", { name: /New folder/ }));
    await user.type(screen.getByRole("textbox", { name: "New folder name" }), "gamma");
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(createDirectory).toHaveBeenCalledWith("/home/x/project", "gamma"));
    expect(await screen.findByRole("button", { name: /gamma/ })).toBeInTheDocument();
  });

  it("shows a rejected mkdir as an error and stays put", async () => {
    const user = userEvent.setup();
    createDirectory.mockResolvedValue(
      listing({ error: "already exists: alpha" }),
    );
    render(<Harness />);
    await screen.findByRole("button", { name: /alpha/ });

    await user.click(screen.getByRole("button", { name: /New folder/ }));
    await user.type(screen.getByRole("textbox", { name: "New folder name" }), "alpha");
    await user.click(screen.getByRole("button", { name: "Create" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("already exists: alpha");
    expect(screen.getByRole("button", { name: /beta-project/ })).toBeInTheDocument();
  });

  it("shows a failed listing with a retry", async () => {
    const user = userEvent.setup();
    listDirectories.mockRejectedValueOnce(new Error("The server is not reachable"));
    listDirectories.mockResolvedValue(listing());
    render(<Harness />);

    expect(await screen.findByText("The server is not reachable")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Retry/ }));

    expect(await screen.findByRole("button", { name: /alpha/ })).toBeInTheDocument();
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
