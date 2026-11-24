import { describe, expect, it } from "vitest";
import { initialState, reduceAll, resetIdCounter } from "../src/lib/transcript";
import type { ServerMessage } from "../src/protocol";
// The mock is the reference recording of a turn, so the reducer is tested against
// the same frames a developer sees in the browser.
import { commandResultFrame, scriptedTurn } from "./script.mjs";

const frames = scriptedTurn() as ServerMessage[];

describe("the mock's scripted turn", () => {
  it("folds into a complete, well-formed transcript", () => {
    resetIdCounter();
    const state = reduceAll(initialState, frames);

    expect(state.turnRunning).toBe(false);
    expect(state.turnId).toBeNull();
    expect(state.status).toBeNull();

    const kinds = state.rows.map((row) => row.kind);
    expect(kinds).toContain("thinking");
    expect(kinds).toContain("assistant");
    expect(kinds).toContain("tool");
    expect(kinds).toContain("bash_notice");
    expect(kinds).toContain("steering");
    expect(kinds).toContain("compaction");
    expect(kinds.at(-1)).toBe("turn_end");
  });

  it("collapses the whole delta stream into a handful of rows", () => {
    resetIdCounter();
    const state = reduceAll(initialState, frames);
    const textDeltas = frames.filter((frame) => frame.type === "text_delta").length;
    const assistantRows = state.rows.filter((row) => row.kind === "assistant").length;
    expect(textDeltas).toBeGreaterThan(10);
    expect(assistantRows).toBeLessThan(textDeltas);
  });

  it("pairs every tool result with its call and marks the failing one", () => {
    resetIdCounter();
    const state = reduceAll(initialState, frames);
    const tools = state.rows.filter((row) => row.kind === "tool");
    expect(tools).toHaveLength(4);
    expect(tools.every((row) => row.kind === "tool" && row.status !== "running")).toBe(true);
    expect(tools.filter((row) => row.kind === "tool" && row.status === "failed")).toHaveLength(1);
  });

  it("carries the image and the action chips through to the row", () => {
    resetIdCounter();
    const state = reduceAll(initialState, frames);
    const withImage = state.rows.filter(
      (row) => row.kind === "tool" && row.images.length > 0,
    );
    expect(withImage).toHaveLength(1);
    const withActions = state.rows.filter(
      (row) => row.kind === "tool" && row.actions !== null,
    );
    expect(withActions.length).toBeGreaterThan(0);
  });

  it("reduces identically when the same turn replays", () => {
    resetIdCounter();
    const once = reduceAll(initialState, frames);
    const twice = reduceAll(once, frames);
    expect(twice.rows.length).toBeGreaterThan(once.rows.length);
    // A second live turn appends; it must never rewrite the first.
    expect(twice.rows.slice(0, once.rows.length).map((row) => row.id)).toEqual(
      once.rows.map((row) => row.id),
    );
  });
});

/**
 * `/help` returns `kind: "text"` with padded columns and a keybinding block. The
 * web client parses that shape (see `lib/commandText.ts`); this keeps the mock
 * honest about what the real command registry actually emits.
 */
describe("the mock's /help reply", () => {
  const frame = commandResultFrame("help") as {
    result: { kind: string; body: string };
  };

  it("is a columnar text body, as the core registry formats it", () => {
    expect(frame.result.kind).toBe("text");
    const lines = frame.result.body.split("\n");
    expect(lines.some((line) => line.includes("Keybindings:"))).toBe(true);
    expect(lines.filter((line) => /^\/\w+/.test(line)).length).toBeGreaterThan(5);
  });

  it("has at least one row where the padded column overflowed", () => {
    expect(frame.result.body).toContain("/settings [key val]Show or change");
  });
});
