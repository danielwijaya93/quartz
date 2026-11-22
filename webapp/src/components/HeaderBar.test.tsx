import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HeaderBar, shortSessionId } from "./HeaderBar";
import { EMPTY_SESSION, type SessionMeta } from "@/lib/transcript";

const SESSION: SessionMeta = {
  ...EMPTY_SESSION,
  sessionId: "01J8ZQ8K4M9V2X3Y5W7T6R0PAB",
  cwd: "/home/daniel/unharness-explore/.worktrees/fix-web-sidebar-first-run",
  model: "claude-sonnet-4-5",
  effort: "high",
};

function renderHeader(overrides: Partial<React.ComponentProps<typeof HeaderBar>> = {}): {
  props: React.ComponentProps<typeof HeaderBar>;
} {
  const props: React.ComponentProps<typeof HeaderBar> = {
    session: SESSION,
    status: "connected",
    richUiMode: "markdown",
    onRichUiModeChange: vi.fn(),
    onNewSession: vi.fn(),
    sidebarOpen: true,
    onToggleSidebar: vi.fn(),
    ...overrides,
  };
  render(<HeaderBar {...props} />);
  return { props };
}

/** The chip a piece of text lives in, so its classes can be inspected. */
function chipFor(text: string): HTMLElement {
  const chip = screen.getByText(text).closest("li");
  if (chip === null) throw new Error(`no chip around ${text}`);
  return chip;
}

describe("HeaderBar", () => {
  it("shows the folder's own name, not a two-letter stub of the path", () => {
    renderHeader();
    // The whole path used to collapse to its last two segments, which for a
    // worktree is "run" or worse.
    expect(screen.getByText("fix-web-sidebar-first-run")).toBeInTheDocument();
    expect(screen.queryByText(/^\.\.\.\//)).not.toBeInTheDocument();
  });

  it("keeps the working directory on a phone, where the model gives way", () => {
    renderHeader();

    const folder = chipFor("fix-web-sidebar-first-run");
    // Never hidden at any width, and on a row of its own: sharing one with the
    // controls left it a few characters wide at laptop sizes.
    expect(folder).not.toHaveClass("hidden");
    expect(folder.closest("header")).not.toBeNull();

    const model = chipFor("claude-sonnet-4-5 · high");
    expect(model).toHaveClass("hidden");
    expect(screen.getByRole("list")).toContainElement(folder);
  });

  it("keeps the full path for hover and for the accessible name", () => {
    renderHeader();

    expect(screen.getByTitle(SESSION.cwd)).toBeInTheDocument();
    expect(screen.getByText(`Working directory ${SESSION.cwd}`)).toBeInTheDocument();
  });

  it("truncates the folder name from the left rather than collapsing", () => {
    renderHeader();
    const label = screen.getByText("fix-web-sidebar-first-run");
    // `dir="rtl"` + `text-left` puts the ellipsis on the left, so the tail of a long
    // name -- the discriminating part -- is what stays readable.
    expect(label).toHaveAttribute("dir", "rtl");
    expect(label).toHaveClass("truncate");
    expect(label).toHaveClass("text-left");
  });

  it("still offers the other chips on a wide screen", () => {
    renderHeader();
    expect(screen.getByText(shortSessionId(SESSION.sessionId))).toBeInTheDocument();
    expect(screen.getByText("claude-sonnet-4-5 · high")).toBeInTheDocument();
  });

  it("says the directory is unknown rather than drawing an empty chip", () => {
    renderHeader({ session: { ...SESSION, cwd: "" } });
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.getByText("Working directory not set")).toBeInTheDocument();
  });

  it("keeps the sidebar toggle and New action labelled", async () => {
    const user = userEvent.setup();
    const { props } = renderHeader({ sidebarOpen: false });

    await user.click(screen.getByRole("button", { name: "Show the session sidebar" }));
    expect(props.onToggleSidebar).toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Start a new session" }));
    expect(props.onNewSession).toHaveBeenCalled();
  });

  it("lets the folder chip trigger onChangeFolder when provided", async () => {
    const user = userEvent.setup();
    const onChangeFolder = vi.fn();
    renderHeader({ onChangeFolder });

    await user.click(
      screen.getByRole("button", { name: `Switch folder (current: ${SESSION.cwd})` }),
    );
    expect(onChangeFolder).toHaveBeenCalledTimes(1);
  });
});
