import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
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
});
