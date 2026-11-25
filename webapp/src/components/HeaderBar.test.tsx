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
    sidebarOpen: true,
    onToggleSidebar: vi.fn(),
    ...overrides,
  };
  render(<HeaderBar {...props} />);
  return { props };
}

describe("HeaderBar", () => {
  it("omits folder selector, new session button, session id, model/effort, and theme toggle", () => {
    renderHeader();
    expect(screen.queryByText("fix-web-sidebar-first-run")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Switch folder/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start a new session" })).not.toBeInTheDocument();
    expect(screen.queryByText(SESSION.sessionId.slice(-8))).not.toBeInTheDocument();
    expect(screen.queryByText(/claude-sonnet-4-5/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Theme:/ })).not.toBeInTheDocument();
  });

  it("keeps the sidebar toggle labelled", async () => {
    const user = userEvent.setup();
    const { props } = renderHeader({ sidebarOpen: false });

    await user.click(screen.getByRole("button", { name: "Show the session sidebar" }));
    expect(props.onToggleSidebar).toHaveBeenCalled();
  });

  it("renders the git branch chip and sandbox toggle visibly on mobile", () => {
    renderHeader({
      session: { ...SESSION, gitBranch: "feat/p0-ui", sandboxed: true },
      onToggleSandbox: vi.fn(),
    });
    const branchChip = screen.getByLabelText("Git branch feat/p0-ui");
    expect(branchChip).toBeInTheDocument();
    expect(branchChip.closest("li")).not.toHaveClass("hidden");
    expect(screen.getByText("feat/p0-ui")).toBeInTheDocument();

    const sandboxBtn = screen.getByRole("button", { name: "Sandbox enabled — click to disable" });
    expect(sandboxBtn.closest("li")).not.toHaveClass("hidden");
    expect(screen.getByText("Sandboxed")).toHaveClass("hidden", "sm:inline");
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
