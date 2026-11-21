import * as React from "react";
import { describe, expect, it } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TurnTray } from "./TurnTray";
import {
  groupTurns,
  isTurn,
  type AssistantRow,
  type Row,
  type ThinkingRow,
  type ToolRow,
  type Turn,
} from "@/lib/transcript";

const assistant = (id: string, text: string): AssistantRow => ({
  kind: "assistant",
  id,
  text,
  formState: null,
});

const thinking = (id: string, text: string): ThinkingRow => ({ kind: "thinking", id, text });

const tool = (id: string, status: ToolRow["status"] = "ok", code = `run_${id}()`): ToolRow => ({
  kind: "tool",
  id,
  code,
  status,
  output: "",
  durationMs: null,
  images: [],
  actions: null,
});

/**
 * Build the turn the way the transcript does, rather than hand-assembling one:
 * a tray is only ever rendered over `groupTurns` output, and a hand-built turn
 * could pass while the grouping it depends on is broken.
 */
function turnOf(rows: Row[], running = false): Turn {
  const turn = groupTurns(rows, { running }).find(isTurn);
  if (turn === undefined) throw new Error("test rows produced no turn");
  return turn;
}

function tray(turn: Turn): React.ReactElement {
  return (
    <TurnTray turn={turn} richUi={false} onPrompt={() => {}} onFormState={() => {}} />
  );
}

describe("TurnTray", () => {
  it("is collapsed by default, with its header wired to the region it controls", async () => {
    const user = userEvent.setup();
    render(tray(turnOf([thinking("k1", "a"), tool("t1"), assistant("a1", "answer")])));

    const header = screen.getByRole("button", { name: /2 steps/ });
    expect(header).toHaveAttribute("aria-expanded", "false");

    const panelId = header.getAttribute("aria-controls");
    expect(panelId).toBeTruthy();
    const panel = document.getElementById(String(panelId));
    expect(panel).toHaveAttribute("role", "region");
    expect(panel).not.toBeVisible();
    // Nothing inside is mounted while closed: a fresh page never pays for a
    // transcript's worth of tool output.
    expect(screen.queryByRole("button", { name: /Used 1 tool/ })).toBeNull();

    await user.click(header);
    expect(header).toHaveAttribute("aria-expanded", "true");
    expect(panel).toBeVisible();
    expect(screen.getByRole("button", { name: /Used 1 tool/ })).toBeInTheDocument();
  });

  it("never opens itself, not even when the turn settles", () => {
    const { rerender } = render(tray(turnOf([thinking("k1", "a"), tool("t1")], true)));
    const header = screen.getByRole("button", { name: /Used 1 tool/ });
    expect(header).toHaveAttribute("aria-expanded", "false");

    rerender(tray(turnOf([thinking("k1", "a"), tool("t1"), assistant("a1", "done")])));
    expect(screen.getByRole("button", { name: /2 steps/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });

  it("shows the current step in its header while the turn runs", () => {
    const { rerender } = render(tray(turnOf([thinking("k1", "a")], true)));
    expect(screen.getByRole("button", { name: /Thinking/ })).toBeInTheDocument();

    // One busy line per turn: the tool's own short label, not a spinner per call.
    rerender(tray(turnOf([thinking("k1", "a"), tool("t1", "running", "read_csv('x')")], true)));
    expect(screen.getByRole("button", { name: /Running read_csv/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /2 steps/ })).toBeNull();
  });

  it("settles to a short count", () => {
    render(
      tray(
        turnOf([
          thinking("k1", "a"),
          tool("t1"),
          tool("t2"),
          assistant("a1", "narration"),
          thinking("k2", "b"),
          assistant("a2", "answer"),
        ]),
      ),
    );
    expect(screen.getByRole("button", { name: /4 steps/ })).toBeInTheDocument();
  });

  it("surfaces failed tools in the collapsed header, without expanding", () => {
    render(
      tray(
        turnOf([
          tool("t1", "failed"),
          tool("t2", "failed"),
          tool("t3"),
          assistant("a1", "answer"),
        ]),
      ),
    );

    // The count is part of the header's accessible name: an aria-label would
    // have hidden it from a screen reader.
    const header = screen.getByRole("button", { name: /1 step.*2 failed/ });
    expect(header).toHaveAttribute("aria-expanded", "false");
    const failed = within(header).getByText("2 failed");
    // Silent failure is the one thing this design may not do, so the count is
    // colour-coded as well as counted.
    expect(failed.className).toContain("text-danger");
  });

  it("keeps one step open at a time", async () => {
    const user = userEvent.setup();
    render(
      tray(
        turnOf([
          tool("t1"),
          assistant("a1", "one down"),
          tool("t2"),
          assistant("a2", "answer"),
        ]),
      ),
    );

    await user.click(screen.getByRole("button", { name: /3 steps/ }));
    const [first, second] = screen.getAllByRole("button", { name: /Used 1 tool/ });
    expect(first).toBeDefined();
    expect(second).toBeDefined();

    await user.click(first!);
    expect(first).toHaveAttribute("aria-expanded", "true");
    // The open run's card is mounted; the closed one's is not.
    expect(screen.getAllByText("Succeeded")).toHaveLength(1);

    await user.click(second!);
    expect(second).toHaveAttribute("aria-expanded", "true");
    expect(first).toHaveAttribute("aria-expanded", "false");
  });

  it("draws a run of thinking rows as one line, not one per row", async () => {
    const user = userEvent.setup();
    render(tray(turnOf([thinking("k1", "first thought"), thinking("k2", "second thought")])));

    // Two rows, one step, one line: the tray folds the boundary the stream made.
    expect(screen.queryByRole("button", { name: /Thinking, 2 lines/ })).toBeNull();
    await user.click(screen.getByRole("button", { name: /1 step/ }));

    const line = screen.getByRole("button", { name: /Thinking, 3 lines/ });
    await user.click(line);
    expect(document.querySelector("pre")?.textContent).toBe("first thought\n\nsecond thought");
  });

  it("closes an open thinking step when another step opens", async () => {
    const user = userEvent.setup();
    render(
      tray(turnOf([thinking("k1", "a"), tool("t1"), assistant("a1", "answer")])),
    );

    await user.click(screen.getByRole("button", { name: /2 steps/ }));
    const thought = screen.getByRole("button", { name: /Thinking, 1 lines/ });
    await user.click(thought);
    expect(thought).toHaveAttribute("aria-expanded", "true");

    await user.click(screen.getByRole("button", { name: /Used 1 tool/ }));
    expect(thought).toHaveAttribute("aria-expanded", "false");
  });

  it("lists the steps in run order and keeps the answer out of the tray", async () => {
    const user = userEvent.setup();
    render(
      tray(
        turnOf([thinking("k1", "a"), tool("t1"), assistant("a1", "The mean is 4.")]),
      ),
    );

    await user.click(screen.getByRole("button", { name: /2 steps/ }));
    const steps = screen.getByTestId("turn-tray-steps");

    expect(steps.children).toHaveLength(2);
    expect(steps.children[0]?.textContent).toContain("Thinking");
    expect(steps.children[1]?.textContent).toContain("Used 1 tool");
    // The answer is the column's, not the tray's.
    expect(screen.queryByText("The mean is 4.")).toBeNull();
    // ...and nothing in the tray repeats the turn's one speaker label.
    expect(screen.queryByText("Quartz")).toBeNull();
  });

  it("announces the current step, not the token stream", () => {
    const { rerender } = render(tray(turnOf([tool("t1")], true)));
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Used 1 tool");

    // The answer streaming below the tray must not reach the live region: that is
    // the per-token firehose the announcer exists to avoid.
    rerender(tray(turnOf([tool("t1"), assistant("a1", "The")], true)));
    expect(screen.getByRole("status")).toHaveTextContent("Used 1 tool");
    rerender(tray(turnOf([tool("t1"), assistant("a1", "The mean is four")], true)));
    expect(screen.getByRole("status").textContent).not.toContain("The mean is four");
  });

  it("names a failure in the live region too, not just in the header", () => {
    render(tray(turnOf([tool("t1", "failed"), tool("t2", "running")], true)));
    expect(screen.getByRole("status")).toHaveTextContent("Running run_t2, 1 failed");
  });

  it("has no live region once the turn settles", () => {
    const { rerender } = render(tray(turnOf([tool("t1")], true)));
    expect(screen.queryByRole("status")).not.toBeNull();

    rerender(tray(turnOf([tool("t1"), assistant("a1", "done")])));
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("follows the newest step while live, unless the reader scrolled up", async () => {
    const user = userEvent.setup();
    const live = (rows: Row[]): Turn => turnOf(rows, true);

    const { rerender } = render(tray(live([thinking("k1", "a")])));
    await user.click(screen.getByRole("button", { name: /Thinking/ }));

    const node = screen.getByTestId("turn-tray-steps");
    expect(node.className).toContain("overflow-y-auto");

    const setMetrics = (scrollTop: number): void => {
      Object.defineProperty(node, "scrollHeight", { configurable: true, value: 600 });
      Object.defineProperty(node, "clientHeight", { configurable: true, value: 200 });
      Object.defineProperty(node, "scrollTop", {
        configurable: true,
        writable: true,
        value: scrollTop,
      });
    };

    // 10px from the bottom counts as pinned, so the next step is followed.
    setMetrics(390);
    fireEvent.scroll(node);
    rerender(tray(live([thinking("k1", "a"), tool("t1")])));
    expect(node.scrollTop).toBe(600);

    // A reader who scrolled up is asking to stop being dragged.
    setMetrics(0);
    fireEvent.scroll(node);
    rerender(tray(live([thinking("k1", "a"), tool("t1"), assistant("a1", "more")])));
    expect(node.scrollTop).toBe(0);
  });
});
