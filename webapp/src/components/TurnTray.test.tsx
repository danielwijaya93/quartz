import * as React from "react";
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
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

function turnOf(rows: Row[], running = false): Turn {
  const turn = groupTurns(rows, { running }).find(isTurn);
  if (turn === undefined) throw new Error("test rows produced no turn");
  return turn;
}

function tray(turn: Turn): React.ReactElement {
  return <TurnTray turn={turn} richUi={false} onPrompt={() => {}} onFormState={() => {}} />;
}

describe("TurnTray", () => {
  it("renders the living waveform and current step label while the turn is live", () => {
    const { rerender } = render(tray(turnOf([thinking("k1", "a")], true)));
    expect(screen.getByTestId("living-waveform")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Thinking");
    expect(screen.queryByRole("button")).toBeNull();

    rerender(tray(turnOf([thinking("k1", "a"), tool("t1", "running", "read_csv('x')")], true)));
    expect(screen.getByTestId("living-waveform")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Running read_csv");

    rerender(tray(turnOf([thinking("k1", "a"), tool("t1", "ok", "read_csv('x')")], true)));
    expect(screen.getByRole("status")).toHaveTextContent("Used 1 tool");
  });

  it("disappears once the turn settles", () => {
    const { container, rerender } = render(
      tray(turnOf([thinking("k1", "a"), tool("t1", "running")], true)),
    );
    expect(screen.getByTestId("living-waveform")).toBeInTheDocument();

    rerender(tray(turnOf([thinking("k1", "a"), tool("t1", "ok"), assistant("a1", "done")], false)));
    expect(container.firstChild).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByTestId("living-waveform")).toBeNull();
  });

  it("disappears as soon as the final reply begins streaming", () => {
    const { container, rerender } = render(
      tray(turnOf([thinking("k1", "a"), tool("t1", "ok")], true)),
    );
    expect(screen.getByTestId("living-waveform")).toBeInTheDocument();

    rerender(tray(turnOf([thinking("k1", "a"), tool("t1", "ok"), assistant("a1", "Hi")], true)));
    expect(container.firstChild).toBeNull();
  });

  it("stays visible if another tool is running after an interim progress update", () => {
    render(
      tray(
        turnOf(
          [
            thinking("k1", "a"),
            tool("t1", "ok", "rg('x')"),
            assistant("a1", "Found #1, checking #2..."),
            tool("t2", "running", "read('b.ts')"),
          ],
          true,
        ),
      ),
    );
    expect(screen.getByTestId("living-waveform")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Running read");
  });

  it("keeps the live status region with the failure count when a running turn has failures", () => {
    render(
      tray(turnOf([tool("t1", "failed"), tool("t2", "running", "retry()")], true)),
    );
    const status = screen.getByTestId("turn-tray-status");
    expect(status).toHaveTextContent("Running retry, 1 failed");
  });
});
