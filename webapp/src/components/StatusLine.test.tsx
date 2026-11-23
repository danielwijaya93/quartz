import { describe, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { StatusLine } from "./StatusLine";

function renderLine(label: string | null, turnRunning: boolean): void {
  render(<StatusLine label={label} status="connected" turnRunning={turnRunning} />);
}

describe("StatusLine", () => {
  it("says nothing while a running turn's tray is already saying it", () => {
    // The tray header is the live line: "Thinking", pulsing, with the transcript's
    // one live region. The server's label for the same moment is not a second line.
    renderLine("Thinking...", true);

    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.queryByText(/Thinking/)).toBeNull();
    expect(screen.queryByText(/running/)).toBeNull();
  });

  it("never appends its own 'running' to the server's label", () => {
    // "Thinking... · running" claimed the turn twice in one line: the tray owns
    // "a turn is running", so the words are gone even when a label survives.
    renderLine("Compacting context…", true);

    const line = screen.getByRole("status");
    expect(line).toHaveTextContent("Compacting context…");
    expect(line.textContent).not.toMatch(/running/);
  });

  it("still shows news the tray cannot have", () => {
    renderLine("Compacting context…", true);
    expect(screen.getByText("Compacting context…")).toBeInTheDocument();
  });

  it("shows a label between turns, when no tray is speaking", () => {
    renderLine("Thinking...", false);
    expect(screen.getByRole("status")).toHaveTextContent("Thinking...");
  });

  it("renders nothing without a label", () => {
    renderLine(null, false);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("hands the slot to the connection banner after the grace delay when the socket is unhappy", () => {
    vi.useFakeTimers();
    try {
      render(<StatusLine label="Thinking..." status="disconnected" turnRunning />);
      // Brief reconnects under 1.5s do not flash the banner.
      expect(screen.queryByRole("status")).toBeNull();
      expect(screen.queryByText(/Thinking/)).toBeNull();

      act(() => {
        vi.advanceTimersByTime(1500);
      });
      expect(screen.getByRole("status")).toHaveTextContent("Disconnected from Quartz");
    } finally {
      vi.useRealTimers();
    }
  });
});
