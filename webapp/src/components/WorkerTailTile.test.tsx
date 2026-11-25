import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { WorkerTailTile } from "./WorkerTailTile";

describe("WorkerTailTile", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("loads and renders live stdout tail for a worker and kills cleanly", async () => {
    const user = userEvent.setup();
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ pid: 4201, tail: "✓ 12 passed\nRunning next..." }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ pid: 4201, killed: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );

    render(
      <WorkerTailTile
        tileNumber={2}
        focused
        maximized={false}
        canClose
        sessionId="sess-1"
        pid="4201"
        label="Quartz worker"
        command="quartz -p worker"
        onFocus={vi.fn()}
        onClose={vi.fn()}
        onToggleMaximize={vi.fn()}
        onPromote={vi.fn()}
        onOpenParentSession={vi.fn()}
      />,
    );

    expect(await screen.findByText(/12 passed/)).toBeInTheDocument();
    expect(fetchSpy).toHaveBeenCalledWith(
      expect.stringContaining("/api/jobs/4201/tail?sessionId=sess-1"),
      expect.anything(),
    );

    await user.click(screen.getByRole("button", { name: "Kill worker" }));
    expect(await screen.findByText("Stopped")).toBeInTheDocument();
    // Final captured output remains visible after stopping
    expect(screen.getByText(/12 passed/)).toBeInTheDocument();
  });

  it("shows Exited state gracefully when job finishes (404) and allows opening parent session", async () => {
    const user = userEvent.setup();
    const onOpenParent = vi.fn();
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify({ error: "Job 999 not found" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      }),
    );

    render(
      <WorkerTailTile
        tileNumber={3}
        focused={false}
        maximized={false}
        canClose
        sessionId="sess-parent"
        pid="999"
        label="pytest"
        command="pytest -q"
        onFocus={vi.fn()}
        onClose={vi.fn()}
        onToggleMaximize={vi.fn()}
        onPromote={vi.fn()}
        onOpenParentSession={onOpenParent}
      />,
    );

    expect(await screen.findByText("Exited")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open parent session" }));
    expect(onOpenParent).toHaveBeenCalledWith("sess-parent");
  });
});
