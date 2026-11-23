import { afterEach, describe, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { JobsTray } from "./JobsTray";

describe("JobsTray", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders nothing when closed", () => {
    const { container } = render(<JobsTray open={false} onClose={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders non-blocking region with jobs, toggles tail, and closes on button click", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            jobs: [{ pid: 42, command: "pytest -q", label: "tests", elapsed_s: 65 }],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ tail: "20 passed" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );

    render(<JobsTray open onClose={onClose} />);
    expect(await screen.findByText(/pid 42 · tests · 1m 5s/)).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();

    await user.click(screen.getByRole("button", { name: "View Tail" }));
    expect(await screen.findByText("20 passed")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Close background jobs" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("ticks elapsed time every second automatically while open", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));

    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          jobs: [{ pid: 99, command: "sleep 30", label: "sleep", elapsed_s: 58 }],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    render(<JobsTray open onClose={vi.fn()} />);

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText(/pid 99 · sleep · 58s/)).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(screen.getByText(/pid 99 · sleep · 1m 1s/)).toBeInTheDocument();

    vi.useRealTimers();
  });
});
