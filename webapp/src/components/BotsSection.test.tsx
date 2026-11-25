import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { BotsSection } from "./BotsSection";
import type { BotConfig, BotRunRecord } from "@/types/bots";

function makeBot(overrides: Partial<BotConfig> = {}): BotConfig {
  return {
    id: "bot-1",
    name: "Issue Triager",
    watcher_type: "github_poll",
    instruction: "Triage incoming issues",
    cwd: "/home/x/repo",
    enabled: true,
    use_worktree: false,
    max_queue_depth: 20,
    github: { repo: "owner/repo", events: ["issues"], interval_seconds: 900 },
    permissions: { auto_allow: ["git status"], require_approval: ["git push"], grace_seconds: 15 },
    ...overrides,
  };
}

describe("BotsSection", () => {
  it("renders empty state cleanly with Create Bot button", async () => {
    const user = userEvent.setup();
    const onCreateBot = vi.fn();

    render(
      <BotsSection
        bots={[]}
        onCreateBot={onCreateBot}
        onEditBot={vi.fn()}
        onToggleBot={vi.fn()}
        onDeleteBot={vi.fn()}
      />,
    );

    expect(screen.getByText("No bots configured")).toBeInTheDocument();
    const createBtn = screen.getByRole("button", { name: /Create Bot/i });
    expect(createBtn.textContent?.trim()).toBe("Create Bot");
    await user.click(createBtn);
    expect(onCreateBot).toHaveBeenCalledTimes(1);
  });

  it("renders bot list with watcher metadata and switch state", () => {
    render(
      <BotsSection
        bots={[
          makeBot({ id: "b1", name: "Issue Triager", enabled: true }),
          makeBot({
            id: "b2",
            name: "Nightly Cron",
            watcher_type: "cron",
            github: null,
            cron: { expression: "*/5 * * * *", timezone: "UTC" },
            enabled: false,
          }),
        ]}
        onCreateBot={vi.fn()}
        onEditBot={vi.fn()}
        onToggleBot={vi.fn()}
        onDeleteBot={vi.fn()}
      />,
    );

    expect(screen.getByText("Issue Triager")).toBeInTheDocument();
    expect(screen.getByText("GitHub · owner/repo")).toBeInTheDocument();
    expect(screen.getByText("Nightly Cron")).toBeInTheDocument();
    expect(screen.getByText("Cron · */5 * * * *")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: /Toggle Issue Triager/i })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("switch", { name: /Toggle Nightly Cron/i })).toHaveAttribute("aria-checked", "false");
  });

  it("invokes toggle, run, edit, and delete callbacks", async () => {
    const user = userEvent.setup();
    const bot = makeBot({ id: "b1", name: "Issue Triager", enabled: true });
    const onToggleBot = vi.fn();
    const onRunBot = vi.fn();
    const onEditBot = vi.fn();
    const onDeleteBot = vi.fn();

    render(
      <BotsSection
        bots={[bot]}
        onCreateBot={vi.fn()}
        onEditBot={onEditBot}
        onToggleBot={onToggleBot}
        onRunBot={onRunBot}
        onDeleteBot={onDeleteBot}
      />,
    );

    const toggle = screen.getByRole("switch", { name: /Toggle Issue Triager/i });
    expect(toggle).toHaveAttribute("aria-checked", "true");
    await user.click(toggle);
    expect(onToggleBot).toHaveBeenCalledWith("b1", false);

    await user.click(screen.getByRole("button", { name: /Run Issue Triager now/i }));
    expect(onRunBot).toHaveBeenCalledWith("b1");

    await user.click(screen.getByRole("button", { name: /Edit Issue Triager/i }));
    expect(onEditBot).toHaveBeenCalledWith(bot);

    await user.click(screen.getByRole("button", { name: /Delete Issue Triager/i }));
    expect(onDeleteBot).toHaveBeenCalledWith("b1");
  });

  it("renders History button, toggling/running disabled state, and toggleError", async () => {
    const user = userEvent.setup();
    const bot = makeBot({ id: "b1", name: "Issue Triager", enabled: true });
    const onOpenHistory = vi.fn();

    render(
      <BotsSection
        bots={[bot]}
        togglingIds={new Set(["b1"])}
        runningIds={new Set(["b1"])}
        toggleError="Failed to toggle bot"
        onCreateBot={vi.fn()}
        onOpenHistory={onOpenHistory}
        onEditBot={vi.fn()}
        onToggleBot={vi.fn()}
        onRunBot={vi.fn()}
        onDeleteBot={vi.fn()}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("Failed to toggle bot");
    expect(screen.getByRole("switch", { name: /Toggle Issue Triager/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Run Issue Triager now/i })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: /Bots history/i }));
    expect(onOpenHistory).toHaveBeenCalledTimes(1);
  });

  it("expands recent runs under a bot, opens a run session, and opens filtered history", async () => {
    const user = userEvent.setup();
    const bot = makeBot({ id: "b1", name: "git-log-summary", watcher_type: "cron", enabled: true });
    const runs: BotRunRecord[] = Array.from({ length: 6 }, (_, i) => ({
      run_id: `r-${i}`,
      bot_id: "b1",
      bot_name: "git-log-summary",
      trigger_id: `t-${i}`,
      event_key: `cron:${i}`,
      source: "cron",
      status: i === 0 ? "ok" : "error",
      started_at: 1700000000 - i * 300,
      finished_at: 1700000004 - i * 300,
      duration_ms: 4200,
      session_id: `sess-${i}`,
      error: i === 0 ? null : "failed",
    }));
    const onOpenSession = vi.fn();
    const onOpenBotHistory = vi.fn();

    render(
      <BotsSection
        bots={[bot]}
        runs={runs}
        activeSessionId="sess-0"
        onCreateBot={vi.fn()}
        onOpenSession={onOpenSession}
        onOpenBotHistory={onOpenBotHistory}
        onEditBot={vi.fn()}
        onToggleBot={vi.fn()}
        onDeleteBot={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: /Toggle runs for git-log-summary/i }));

    // Shows at most 5 recent runs
    const runButtons = screen.getAllByRole("button", { name: /Open run/i });
    expect(runButtons).toHaveLength(5);

    await user.click(runButtons[0]!);
    expect(onOpenSession).toHaveBeenCalledWith("sess-0");

    await user.click(screen.getByRole("button", { name: /View all 6 runs/i }));
    expect(onOpenBotHistory).toHaveBeenCalledWith("b1");
  });
});
