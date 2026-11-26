import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { WatcherBanner } from "./WatcherBanner";
import type { WatcherState } from "../protocol";

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

describe("WatcherBanner", () => {
  it("renders countdown banner with Continue now and Cancel actions", () => {
    const onAction = vi.fn();
    render(
      <WatcherBanner
        watcher={makeWatcher({ status: "countdown", countdownRemaining: 24 })}
        onAction={onAction}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("Turn Watcher continuing in 24s…");
    fireEvent.click(screen.getByRole("button", { name: "Continue now" }));
    expect(onAction).toHaveBeenCalledWith("continue_now");

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onAction).toHaveBeenCalledWith("cancel_countdown");
  });

  it("renders nothing when idle and never renders Watch pill or Pause button", () => {
    const { container } = render(
      <WatcherBanner watcher={makeWatcher({ status: "idle" })} onAction={vi.fn()} />,
    );
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole("button", { name: "Turn Watcher controls" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Pause|Resume/i })).not.toBeInTheDocument();
  });
});
