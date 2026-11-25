import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CreateBotModal } from "./CreateBotModal";
import type { BotConfig } from "@/types/bots";

describe("CreateBotModal", () => {
  it("conditionally renders watcher-specific fields when watcher type changes", async () => {
    const user = userEvent.setup();
    render(<CreateBotModal open={true} onClose={vi.fn()} onSubmit={vi.fn()} />);

    const watcherSelect = screen.getByLabelText(/Watcher Type/i);
    expect(watcherSelect).toHaveValue("github_poll");
    expect(screen.getByLabelText(/GitHub Repo/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Cron Expression/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Webhook Secret/i)).not.toBeInTheDocument();

    await user.selectOptions(watcherSelect, "cron");
    expect(screen.getByLabelText(/Cron Expression/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Timezone/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/GitHub Repo/i)).not.toBeInTheDocument();

    await user.selectOptions(watcherSelect, "webhook");
    expect(screen.getByLabelText(/Webhook Secret/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Event Filter/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Cron Expression/i)).not.toBeInTheDocument();
  });

  it("validates required fields before submitting", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<CreateBotModal open={true} onClose={vi.fn()} onSubmit={onSubmit} />);

    await user.click(screen.getByRole("button", { name: /Save Bot|Create Bot/i }));
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("submits structured payload for github_poll bot", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(
      <CreateBotModal
        open={true}
        defaultCwd="/home/x/project"
        onClose={vi.fn()}
        onSubmit={onSubmit}
      />,
    );

    await user.type(screen.getByLabelText(/Bot Name/i), "PR Reviewer");
    await user.type(screen.getByLabelText(/GitHub Repo/i), "danielwijaya93/quartz");
    await user.clear(screen.getByLabelText(/Events/i));
    await user.type(screen.getByLabelText(/Events/i), "pull_request, issues");
    await user.type(screen.getByLabelText(/Instruction/i), "Review new PRs");
    await user.type(screen.getByLabelText(/Pre-approved Commands/i), "git diff, pytest");

    await user.click(screen.getByRole("button", { name: /Create Bot/i }));

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({
        name: "PR Reviewer",
        watcher_type: "github_poll",
        cwd: "/home/x/project",
        instruction: "Review new PRs",
        enabled: true,
        github: {
          repo: "danielwijaya93/quartz",
          events: ["pull_request", "issues"],
          interval_seconds: 900,
        },
        permissions: expect.objectContaining({
          auto_allow: ["git diff", "pytest"],
        }),
      }),
    );
  });

  it("prefills fields when editing an existing bot", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    const existing: BotConfig = {
      id: "bot-cron",
      name: "Daily Summary",
      watcher_type: "cron",
      instruction: "Summarize repo activity",
      cwd: "/home/x/repo",
      enabled: false,
      use_worktree: true,
      max_queue_depth: 10,
      cron: { expression: "0 9 * * *", timezone: "America/New_York" },
      permissions: { auto_allow: ["git log"], require_approval: ["git push"], grace_seconds: 15 },
    };

    render(
      <CreateBotModal open={true} initialBot={existing} onClose={vi.fn()} onSubmit={onSubmit} />,
    );

    expect(screen.getByLabelText(/Bot Name/i)).toHaveValue("Daily Summary");
    expect(screen.getByLabelText(/Watcher Type/i)).toHaveValue("cron");
    expect(screen.getByLabelText(/Cron Expression/i)).toHaveValue("0 9 * * *");
    expect(screen.getByLabelText(/Timezone/i)).toHaveValue("America/New_York");

    await user.click(screen.getByRole("button", { name: /Save Changes/i }));
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({
        id: "bot-cron",
        name: "Daily Summary",
        watcher_type: "cron",
        enabled: false,
        use_worktree: true,
        cron: { expression: "0 9 * * *", timezone: "America/New_York" },
      }),
    );
  });
});
