import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fireEvent } from "@testing-library/react";
import { HeaderBar } from "./HeaderBar";
import { EMPTY_SESSION, type SessionMeta } from "@/lib/transcript";
import type { WatcherState } from "@/protocol";

function makeWatcher(overrides: Partial<WatcherState> = {}): WatcherState {
  return {
    configured: true,
    provider: "google",
    model: "gemini-2.5-flash",
    mode: "auto",
    status: "idle",
    goal: "",
    promptTemplate: "Continue with the next step you mentioned.",
    delaySeconds: 30,
    maxTurns: 10,
    turnCount: 0,
    nudgesUsed: 0,
    maxNudges: 3,
    paused: false,
    countdownRemaining: 0,
    lastReason: "",
    ...overrides,
  };
}

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

  it("shows Sandbox Off in red without strikethrough when sandbox is off and toggles on click", async () => {
    const user = userEvent.setup();
    const onToggleSandbox = vi.fn();
    renderHeader({ session: { ...SESSION, sandboxed: false }, onToggleSandbox });

    const btn = screen.getByRole("button", { name: "Sandbox disabled — click to enable" });
    expect(btn).toHaveAttribute("aria-pressed", "false");
    const label = screen.getByText("Sandbox Off");
    expect(label).toHaveClass("text-danger");
    expect(label).not.toHaveClass("line-through");

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

  it("renders simplified watcher pill without 'Watch' or Pause button and portals settings dialog", async () => {
    const onConfigureWatcher = vi.fn();
    renderHeader({
      watcher: makeWatcher({ mode: "auto" }),
      onConfigureWatcher,
      onWatcherAction: vi.fn(),
    });

    const pill = screen.getByRole("button", { name: "Turn Watcher controls" });
    expect(pill).toHaveTextContent("Auto");
    expect(pill).not.toHaveTextContent(/Watch/i);
    expect(screen.queryByRole("button", { name: /Pause|Resume/i })).not.toBeInTheDocument();

    fireEvent.click(pill);
    const dialog = screen.getByRole("dialog", { name: "Session Turn Watcher Settings" });
    expect(dialog).toBeInTheDocument();
    expect(dialog.closest("header")).toBeNull();
    fireEvent.pointerDown(dialog);
    expect(screen.getByRole("dialog", { name: "Session Turn Watcher Settings" })).toBeInTheDocument();

    await userEvent.setup().click(screen.getByRole("button", { name: /Session watcher mode/i }));
    await userEvent.setup().click(screen.getByRole("option", { name: "Goal (/goal)" }));
    fireEvent.change(screen.getByLabelText("Session goal"), { target: { value: "Make tests green" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    expect(onConfigureWatcher).toHaveBeenCalledWith(
      expect.objectContaining({
        mode: "goal",
        goal: "Make tests green",
      }),
    );
  });

  it("shows 'Sandbox Off' without strikethrough when sandbox is disabled", () => {
    renderHeader({
      session: { ...SESSION, sandboxed: false },
      onToggleSandbox: vi.fn(),
    });
    const label = screen.getByText("Sandbox Off");
    expect(label).toBeInTheDocument();
    expect(label).not.toHaveClass("line-through");
    expect(screen.queryByText("Sandboxed")).not.toBeInTheDocument();
  });
});
