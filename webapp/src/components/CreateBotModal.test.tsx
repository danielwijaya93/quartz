import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CreateBotModal } from "./CreateBotModal";
import type { BotConfig } from "@/types/bots";
import * as modelsApi from "@/lib/modelsApi";

vi.mock("@/lib/modelsApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/modelsApi")>();
  return {
    ...actual,
    fetchModels: vi.fn(),
  };
});

const MOCK_MODELS: modelsApi.ModelsPayload = {
  current: { provider: "google", model: "gemini-2.5-flash", effort: "medium" },
  providers: [
    {
      id: "google",
      label: "Google Gemini",
      ready: true,
      status: "Ready",
      models: ["gemini-2.5-flash", "gemini-2.5-pro"],
      efforts: ["low", "medium", "high"],
      defaultModel: "gemini-2.5-flash",
      defaultEffort: "medium",
    },
    {
      id: "openai",
      label: "OpenAI",
      ready: true,
      status: "Ready",
      models: ["gpt-4o", "o3"],
      efforts: ["low", "medium", "high"],
      modelEfforts: { o3: ["medium", "high"] },
      defaultModel: "gpt-4o",
      defaultEffort: "medium",
    },
  ],
};

beforeEach(() => {
  vi.mocked(modelsApi.fetchModels).mockResolvedValue(MOCK_MODELS);
});

describe("CreateBotModal", () => {
  it("conditionally renders watcher-specific fields when watcher type changes", async () => {
    const user = userEvent.setup();
    render(<CreateBotModal open={true} onClose={vi.fn()} onSubmit={vi.fn()} />);

    const watcherTrigger = screen.getByRole("button", { name: /Watcher Type/i });
    expect(watcherTrigger).toHaveTextContent("GitHub Poll");
    expect(screen.getByLabelText(/GitHub Repo/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Cron Expression/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Webhook Secret/i)).not.toBeInTheDocument();

    await user.click(watcherTrigger);
    await user.click(screen.getByRole("option", { name: "Cron" }));
    expect(screen.getByLabelText(/Cron Expression/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Timezone/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/GitHub Repo/i)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Watcher Type/i }));
    await user.click(screen.getByRole("option", { name: "Webhook" }));
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
    expect(screen.getByRole("button", { name: /Watcher Type/i })).toHaveTextContent("Cron");
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

  it("allows selecting provider, model, and effort on create and resetting on edit", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();

    const { rerender } = render(
      <CreateBotModal open={true} defaultCwd="/tmp/repo" onClose={vi.fn()} onSubmit={onSubmit} />,
    );

    const providerTrigger = await screen.findByRole("button", { name: /Provider/i });
    await user.click(providerTrigger);
    await user.click(await screen.findByRole("option", { name: /OpenAI/i }));

    await user.click(screen.getByRole("button", { name: /Model/i }));
    await user.click(screen.getByRole("option", { name: "o3" }));

    await user.click(screen.getByRole("button", { name: /Effort/i }));
    await user.click(screen.getByRole("option", { name: "high" }));

    await user.type(screen.getByLabelText(/Bot Name/i), "Model Bot");
    await user.type(screen.getByLabelText(/GitHub Repo/i), "danielwijaya93/quartz");
    await user.type(screen.getByLabelText(/Instruction/i), "Check PRs");
    await user.click(screen.getByRole("button", { name: /Create Bot/i }));

    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({
        provider: "openai",
        model: "o3",
        effort: "high",
      }),
    );

    onSubmit.mockClear();
    const existing: BotConfig = {
      id: "bot-model",
      name: "Model Bot",
      watcher_type: "cron",
      instruction: "Check repo",
      cwd: "/tmp/repo",
      provider: "openai",
      model: "o3",
      effort: "high",
      enabled: true,
      use_worktree: false,
      max_queue_depth: 20,
      cron: { expression: "0 9 * * *", timezone: "UTC" },
      permissions: { auto_allow: [], require_approval: ["git push"], grace_seconds: 15 },
    };

    rerender(
      <CreateBotModal open={true} initialBot={existing} onClose={vi.fn()} onSubmit={onSubmit} />,
    );

    expect(screen.getByRole("button", { name: /Provider/i })).toHaveTextContent(/OpenAI/i);
    expect(screen.getByRole("button", { name: /Model/i })).toHaveTextContent("o3");
    expect(screen.getByRole("button", { name: /Effort/i })).toHaveTextContent("high");

    // Reset provider back to Default (inherit)
    await user.click(screen.getByRole("button", { name: /Provider/i }));
    await user.click(screen.getByRole("option", { name: "Default (inherit)" }));
    await user.click(screen.getByRole("button", { name: /Save Changes/i }));

    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({
        provider: null,
        model: null,
        effort: null,
      }),
    );
  });

  it("submits per-bot governance guardrails (budget, max errors, webhook rate limit)", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(<CreateBotModal open={true} onClose={vi.fn()} onSubmit={onSubmit} />);

    await user.type(screen.getByLabelText(/Bot Name/i), "Guarded Bot");
    await user.click(screen.getByRole("button", { name: /Watcher Type/i }));
    await user.click(screen.getByRole("option", { name: "Cron" }));
    await user.type(screen.getByLabelText(/Cron Expression/i), "0 * * * *");
    await user.type(screen.getByLabelText(/Instruction/i), "check health");
    await user.type(screen.getByLabelText(/Daily USD Budget/i), "2.5");
    await user.type(screen.getByLabelText(/Max Errors/i), "3");
    await user.type(screen.getByLabelText(/Rate Limit/i), "20");

    await user.click(screen.getByRole("button", { name: /Create Bot/i }));

    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({
        name: "Guarded Bot",
        max_usd_per_day: 2.5,
        max_consecutive_errors: 3,
        webhook_rate_limit_per_minute: 20,
      }),
    );
  });
});
