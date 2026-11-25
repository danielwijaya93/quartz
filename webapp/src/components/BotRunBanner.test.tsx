import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { BotRunBanner } from "./BotRunBanner";
import type { BotConfig, BotRunRecord } from "@/types/bots";

describe("BotRunBanner", () => {
  it("renders run metadata and triggers All Runs, Edit Bot, and Re-run Now", async () => {
    const user = userEvent.setup();
    const run: BotRunRecord = {
      run_id: "r1",
      bot_id: "b1",
      bot_name: "git-log-summary",
      trigger_id: "t1",
      event_key: "cron:1",
      source: "cron",
      status: "ok",
      started_at: 1700000000,
      finished_at: 1700000004.2,
      duration_ms: 4200,
      session_id: "sess-1",
      error: null,
    };
    const bot: BotConfig = {
      id: "b1",
      name: "git-log-summary",
      watcher_type: "cron",
      instruction: "Check git logs",
      cwd: null,
      enabled: true,
      use_worktree: false,
      max_queue_depth: 10,
      github: null,
      cron: { expression: "*/5 * * * *", timezone: "UTC" },
      webhook: null,
      permissions: { auto_allow: [], require_approval: [], grace_seconds: 60 },
      created_at: 1700000000,
      updated_at: 1700000000,
    };

    const onOpenHistory = vi.fn();
    const onEditBot = vi.fn();
    const onRerun = vi.fn();

    render(
      <BotRunBanner
        run={run}
        bot={bot}
        onOpenHistory={onOpenHistory}
        onEditBot={onEditBot}
        onRerun={onRerun}
      />,
    );

    expect(screen.getByText("git-log-summary")).toBeInTheDocument();
    expect(screen.getByText(/Success/i)).toBeInTheDocument();
    expect(screen.getByText(/4\.2s/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /All Runs/i }));
    expect(onOpenHistory).toHaveBeenCalledWith("b1");

    await user.click(screen.getByRole("button", { name: /Edit Bot/i }));
    expect(onEditBot).toHaveBeenCalledWith(bot);

    await user.click(screen.getByRole("button", { name: /Re-run Now/i }));
    expect(onRerun).toHaveBeenCalledWith("b1");
  });
});
