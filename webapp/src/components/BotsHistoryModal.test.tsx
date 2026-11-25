import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { BotsHistoryModal } from "./BotsHistoryModal";
import * as botsApi from "@/lib/botsApi";

vi.mock("@/lib/botsApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/botsApi")>();
  return {
    ...actual,
    fetchBotsHistory: vi.fn(),
  };
});

describe("BotsHistoryModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders run history and deleted bots, opens session, and restores deleted bot", async () => {
    const user = userEvent.setup();
    vi.mocked(botsApi.fetchBotsHistory).mockResolvedValue({
      runs: [
        {
          run_id: "r1",
          bot_id: "b1",
          bot_name: "Nightly Check",
          trigger_id: "t1",
          event_key: "cron:2026-11-25T02:00",
          source: "cron",
          status: "ok",
          started_at: 1700000000,
          finished_at: 1700000002,
          duration_ms: 2000,
          session_id: "sess-123",
          error: null,
        },
      ],
      deletedBots: [
        {
          bot: {
            id: "b-del",
            name: "Old Triager",
            watcher_type: "github_poll",
            instruction: "Triage",
            enabled: true,
            use_worktree: false,
            max_queue_depth: 20,
            permissions: { auto_allow: [], require_approval: [], grace_seconds: 15 },
          },
          deleted_at: 1700000100,
        },
      ],
    });

    const onClose = vi.fn();
    const onOpenSession = vi.fn();
    const onRestoreBot = vi.fn().mockResolvedValue(undefined);

    render(
      <BotsHistoryModal
        open={true}
        bots={[]}
        onClose={onClose}
        onOpenSession={onOpenSession}
        onRestoreBot={onRestoreBot}
      />,
    );

    await waitFor(() => {
      expect(screen.getAllByText("Nightly Check").length).toBeGreaterThanOrEqual(1);
    });
    expect(screen.getAllByText("Success").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("2.0s")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Open session/i }));
    expect(onOpenSession).toHaveBeenCalledWith("sess-123");
    expect(onClose).toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: /Deleted Bots \(1\)/i }));
    expect(screen.getByText("Old Triager")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Restore/i }));
    expect(onRestoreBot).toHaveBeenCalledWith("b-del");
  });
});
