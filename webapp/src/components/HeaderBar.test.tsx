import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HeaderBar } from "./HeaderBar";
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
    expect(screen.getByText("fix-web-sidebar-first-run")).toBeInTheDocument();
    expect(screen.queryByText(/^\.\.\.\//)).not.toBeInTheDocument();
  });

  it("keeps the working directory visible at all widths", () => {
    renderHeader();

    const folder = chipFor("fix-web-sidebar-first-run");
    expect(folder).not.toHaveClass("hidden");
    expect(folder.closest("header")).not.toBeNull();
    expect(screen.getByRole("list")).toContainElement(folder);
  });

  it("does not render session id, model/effort, or theme toggle in the top bar", () => {
    renderHeader();
    expect(screen.queryByText(SESSION.sessionId.slice(-8))).not.toBeInTheDocument();
    expect(screen.queryByText(/claude-sonnet-4-5/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Theme:/ })).not.toBeInTheDocument();
  });

  it("keeps the full path for hover and for the accessible name", () => {
    renderHeader();

    expect(screen.getByTitle(SESSION.cwd)).toBeInTheDocument();
    expect(screen.getByText(`Working directory ${SESSION.cwd}`)).toBeInTheDocument();
  });

  it("truncates the folder name from the left rather than collapsing", () => {
    renderHeader();
    const label = screen.getByText("fix-web-sidebar-first-run");
    expect(label).toHaveAttribute("dir", "rtl");
    expect(label).toHaveClass("truncate");
    expect(label).toHaveClass("text-left");
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

  it("renders the git branch chip when gitBranch is present", () => {
    renderHeader({ session: { ...SESSION, gitBranch: "feat/p0-ui" } });
    expect(screen.getByLabelText("Git branch feat/p0-ui")).toBeInTheDocument();
    expect(screen.getByText("feat/p0-ui")).toBeInTheDocument();
  });

  it("shows Sandboxed in red with strikethrough when sandbox is off and toggles on click", async () => {
    const user = userEvent.setup();
    const onToggleSandbox = vi.fn();
    renderHeader({ session: { ...SESSION, sandboxed: false }, onToggleSandbox });

    const btn = screen.getByRole("button", { name: "Sandbox disabled — click to enable" });
    expect(btn).toHaveAttribute("aria-pressed", "false");
    const label = screen.getByText("Sandboxed");
    expect(label).toHaveClass("text-danger", "line-through");

    await user.click(btn);
    expect(onToggleSandbox).toHaveBeenCalledTimes(1);
  });

  it("shows Sandboxed without strikethrough when sandbox is on and disables toggle during a turn", () => {
    const onToggleSandbox = vi.fn();
    renderHeader({
      session: { ...SESSION, sandboxed: true },
      onToggleSandbox,
      turnRunning: true,
    });

    const btn = screen.getByRole("button", { name: "Sandbox enabled — click to disable" });
    expect(btn).toHaveAttribute("aria-pressed", "true");
    expect(btn).toBeDisabled();
    const label = screen.getByText("Sandboxed");
    expect(label).not.toHaveClass("line-through");
    expect(label).not.toHaveClass("text-danger");
  });
});
