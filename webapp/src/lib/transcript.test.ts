import { describe, expect, it, beforeEach } from "vitest";
import {
  addUserPrompt,
  approxTokens,
  initialState,
  lineCount,
  reduceAll,
  resetIdCounter,
  type TranscriptState,
} from "./transcript";
import type { ServerMessage } from "../protocol";

const fold = (messages: ServerMessage[], from: TranscriptState = initialState): TranscriptState =>
  reduceAll(from, messages);

beforeEach(() => {
  resetIdCounter();
});

describe("text_delta", () => {
  it("accumulates many deltas into one assistant row", () => {
    const state = fold([
      { type: "turn_start", turnId: "t1" },
      { type: "text_delta", text: "Hello" },
      { type: "text_delta", text: ", " },
      { type: "text_delta", text: "world" },
    ]);
    const assistant = state.rows.filter((row) => row.kind === "assistant");
    expect(assistant).toHaveLength(1);
    expect(assistant[0]).toMatchObject({ kind: "assistant", text: "Hello, world" });
  });

  it("never appends a turn's deltas into the previous turn's row", () => {
    const state = fold([
      { type: "turn_start", turnId: "t1" },
      { type: "text_delta", text: "turn one" },
      { type: "turn_end", status: "ok", tokens: 1, elapsedMs: 1 },
      { type: "turn_start", turnId: "t2" },
      { type: "text_delta", text: "turn two" },
    ]);
    const texts = state.rows.filter((row) => row.kind === "assistant").map((row) => row.text);
    expect(texts).toEqual(["turn one", "turn two"]);
  });

  it("never appends a delta into a replayed history row", () => {
    const replayed = fold([
      {
        type: "session_state",
        sessionId: "s",
        cwd: "/r",
        model: "m",
        effort: "e",
        turnRunning: false,
        commands: [],
        messages: [{ role: "assistant", content: "old answer" }],
      },
      { type: "turn_start", turnId: "t1" },
      { type: "text_delta", text: "new answer" },
    ]);
    const texts = replayed.rows.filter((row) => row.kind === "assistant").map((row) => row.text);
    expect(texts).toEqual(["old answer", "new answer"]);
  });

  it("opens a new assistant row after an intervening row kind", () => {
    const state = fold([
      { type: "turn_start", turnId: "t1" },
      { type: "text_delta", text: "first" },
      { type: "bash_notice", text: "job done" },
      { type: "text_delta", text: "second" },
    ]);
    const texts = state.rows.filter((row) => row.kind === "assistant").map((row) => row.text);
    expect(texts).toEqual(["first", "second"]);
  });

  it("keeps thinking and text as separate rows when interleaved", () => {
    const state = fold([
      { type: "turn_start", turnId: "t1" },
      { type: "thinking_delta", text: "let me " },
      { type: "thinking_delta", text: "consider" },
      { type: "text_delta", text: "The answer" },
      { type: "thinking_delta", text: "more thought" },
      { type: "text_delta", text: " is 42." },
    ]);
    expect(state.rows.map((row) => row.kind)).toEqual([
      "thinking",
      "assistant",
      "thinking",
      "assistant",
    ]);
    expect(state.rows[0]).toMatchObject({ text: "let me consider" });
    expect(state.rows[1]).toMatchObject({ text: "The answer" });
    expect(state.rows[2]).toMatchObject({ text: "more thought" });
    expect(state.rows[3]).toMatchObject({ text: " is 42." });
  });
});

describe("tool calls", () => {
  it("pairs tool_result to tool_call by id", () => {
    const state = fold([
      { type: "turn_start", turnId: "t1" },
      { type: "tool_call", id: "c1", code: "print(1)" },
      {
        type: "tool_result",
        id: "c1",
        ok: true,
        output: "1",
        durationMs: 5,
        images: [],
        actions: null,
      },
    ]);
    const tools = state.rows.filter((row) => row.kind === "tool");
    expect(tools).toHaveLength(1);
    expect(tools[0]).toMatchObject({
      kind: "tool",
      id: "c1",
      code: "print(1)",
      status: "ok",
      output: "1",
      durationMs: 5,
    });
  });

  it("marks a failed result as failed and keeps its output", () => {
    const state = fold([
      { type: "turn_start", turnId: "t1" },
      { type: "tool_call", id: "c1", code: "boom()" },
      {
        type: "tool_result",
        id: "c1",
        ok: false,
        output: "NameError",
        durationMs: 2,
        images: [],
        actions: null,
      },
    ]);
    expect(state.rows[0]).toMatchObject({ status: "failed", output: "NameError" });
  });

  it("does not crash on a result with no preceding call, and shows the output", () => {
    const state = fold([
      { type: "turn_start", turnId: "t1" },
      {
        type: "tool_result",
        id: "orphan",
        ok: true,
        output: "late output",
        durationMs: 9,
        images: [],
        actions: { wrote: "x.txt" },
      },
    ]);
    const tool = state.rows.find((row) => row.kind === "tool");
    expect(tool).toMatchObject({
      id: "orphan",
      status: "ok",
      output: "late output",
      code: "",
      actions: { wrote: "x.txt" },
    });
  });

  it("carries images and actions through", () => {
    const state = fold([
      { type: "turn_start", turnId: "t1" },
      { type: "tool_call", id: "c1", code: "fig" },
      {
        type: "tool_result",
        id: "c1",
        ok: true,
        output: "Figure",
        durationMs: 1,
        images: [{ mimeType: "image/png", data: "abc" }],
        actions: { wrote: "plot.png" },
      },
    ]);
    expect(state.rows[0]).toMatchObject({
      images: [{ mimeType: "image/png", data: "abc" }],
      actions: { wrote: "plot.png" },
    });
  });

  it("ignores a second result for the same id instead of duplicating the row", () => {
    const state = fold([
      { type: "turn_start", turnId: "t1" },
      { type: "tool_call", id: "c1", code: "x" },
      { type: "tool_result", id: "c1", ok: true, output: "a", durationMs: 1, images: [], actions: null },
      { type: "tool_result", id: "c1", ok: false, output: "b", durationMs: 2, images: [], actions: null },
    ]);
    expect(state.rows.filter((row) => row.kind === "tool")).toHaveLength(1);
    expect(state.rows[0]).toMatchObject({ status: "failed", output: "b" });
  });
});

describe("turn lifecycle", () => {
  it("brackets a turn and clears running state at turn_end", () => {
    const started = fold([{ type: "turn_start", turnId: "t1" }]);
    expect(started.turnRunning).toBe(true);
    expect(started.turnId).toBe("t1");

    const ended = fold(
      [{ type: "turn_end", status: "ok", tokens: 12, elapsedMs: 300 }],
      started,
    );
    expect(ended.turnRunning).toBe(false);
    expect(ended.turnId).toBeNull();
    expect(ended.rows.at(-1)).toMatchObject({
      kind: "turn_end",
      status: "ok",
      tokens: 12,
      elapsedMs: 300,
    });
  });

  it("appends a second turn rather than replacing the first", () => {
    const state = fold([
      { type: "turn_start", turnId: "t1" },
      { type: "text_delta", text: "one" },
      { type: "turn_end", status: "ok", tokens: 1, elapsedMs: 1 },
      { type: "turn_start", turnId: "t2" },
      { type: "text_delta", text: "two" },
      { type: "turn_end", status: "ok", tokens: 2, elapsedMs: 2 },
    ]);
    expect(state.rows.filter((row) => row.kind === "assistant").map((row) => row.text)).toEqual([
      "one",
      "two",
    ]);
    expect(state.rows.filter((row) => row.kind === "turn_end")).toHaveLength(2);
  });

  it("carries an error string on a failed turn_end", () => {
    const state = fold([
      { type: "turn_start", turnId: "t1" },
      { type: "turn_end", status: "error", tokens: 0, elapsedMs: 5, error: "provider 500" },
    ]);
    expect(state.rows.at(-1)).toMatchObject({ status: "error", error: "provider 500" });
  });

  it("records aborted and interrupted statuses distinctly", () => {
    const aborted = fold([
      { type: "turn_start", turnId: "t1" },
      { type: "turn_end", status: "aborted", tokens: 0, elapsedMs: 1 },
    ]);
    expect(aborted.rows.at(-1)).toMatchObject({ status: "aborted" });

    const interrupted = fold([
      { type: "turn_start", turnId: "t2" },
      { type: "turn_end", status: "interrupted", tokens: 0, elapsedMs: 1 },
    ]);
    expect(interrupted.rows.at(-1)).toMatchObject({ status: "interrupted" });
  });
});

describe("system rows", () => {
  it("emits a compaction row with both sides of the boundary", () => {
    const state = fold([
      { type: "compaction", summary: "summarised", messagesBefore: 30, messagesAfter: 8, tokensBefore: 1000, tokensAfter: 250 },
    ]);
    expect(state.rows).toHaveLength(1);
    expect(state.rows[0]).toMatchObject({
      kind: "compaction",
      summary: "summarised",
      messagesBefore: 30,
      messagesAfter: 8,
      tokensBefore: 1000,
      tokensAfter: 250,
    });
  });

  it("emits a bash_notice row", () => {
    const state = fold([{ type: "bash_notice", text: "job 3 finished" }]);
    expect(state.rows[0]).toMatchObject({ kind: "bash_notice", text: "job 3 finished" });
  });

  it("emits a steering row", () => {
    const state = fold([{ type: "steering", text: "also check x" }]);
    expect(state.rows[0]).toMatchObject({ kind: "steering", text: "also check x" });
  });
});

describe("status", () => {
  it("sets the label and clears it on null", () => {
    const withLabel = fold([{ type: "status", label: "Thinking..." }]);
    expect(withLabel.status).toBe("Thinking...");
    const cleared = fold([{ type: "status", label: null }], withLabel);
    expect(cleared.status).toBeNull();
  });

  it("resets status when a new turn starts", () => {
    const state = fold([
      { type: "status", label: "Thinking..." },
      { type: "turn_start", turnId: "t1" },
    ]);
    expect(state.status).toBeNull();
  });

  it("clears status at turn_end", () => {
    const state = fold([
      { type: "turn_start", turnId: "t1" },
      { type: "status", label: "Working" },
      { type: "turn_end", status: "ok", tokens: 0, elapsedMs: 1 },
    ]);
    expect(state.status).toBeNull();
  });
});

describe("errors", () => {
  it("renders a non-fatal error inline without latching", () => {
    const state = fold([{ type: "error", message: "turn already running", fatal: false }]);
    expect(state.rows[0]).toMatchObject({ kind: "error", fatal: false });
    expect(state.fatalError).toBeNull();
  });

  it("latches a fatal error so the composer can block", () => {
    const state = fold([{ type: "error", message: "auth failed", fatal: true }]);
    expect(state.fatalError).toBe("auth failed");
  });
});

describe("command_result", () => {
  const kinds = ["text", "list", "error", "needs_terminal"] as const;

  for (const kind of kinds) {
    it(`renders kind="${kind}" as a command row`, () => {
      const state = fold([
        {
          type: "command_result",
          name: "/settings",
          ok: kind !== "error",
          result: { kind, title: "T", body: "B", items: [{ label: "a" }], message: "M" },
        },
      ]);
      expect(state.rows).toHaveLength(1);
      expect(state.rows[0]).toMatchObject({
        kind: "command",
        name: "settings",
        ok: kind !== "error",
        result: { kind },
      });
    });
  }

  it("strips a leading slash from the echoed name", () => {
    const state = fold([
      { type: "command_result", name: "usage", ok: true, result: { kind: "text" } },
    ]);
    expect(state.rows[0]).toMatchObject({ name: "usage" });
  });

  it("keeps list items and needs_terminal messages intact", () => {
    const list = fold([
      {
        type: "command_result",
        name: "jobs",
        ok: true,
        result: { kind: "list", items: [{ label: "3", detail: "exit 0" }] },
      },
    ]);
    expect(list.rows[0]).toMatchObject({ result: { items: [{ label: "3", detail: "exit 0" }] } });

    const needs = fold([
      {
        type: "command_result",
        name: "model",
        ok: true,
        result: { kind: "needs_terminal", message: "run in terminal" },
      },
    ]);
    expect(needs.rows[0]).toMatchObject({
      result: { kind: "needs_terminal", message: "run in terminal" },
    });
  });
});

describe("session_state and reconnect", () => {
  const history: ServerMessage = {
    type: "session_state",
    sessionId: "s-1",
    cwd: "/repo",
    model: "m",
    effort: "medium",
    turnRunning: false,
    commands: [{ name: "help", description: "Show help" }],
    messages: [
      { role: "user", content: "first prompt" },
      { role: "assistant", content: "first answer" },
      { role: "user", content: "second prompt" },
      { role: "assistant", content: "second answer" },
    ],
  };

  it("hydrates rows, session meta and commands", () => {
    const state = fold([history]);
    expect(state.session).toMatchObject({ sessionId: "s-1", model: "m", effort: "medium" });
    expect(state.session.commands).toHaveLength(1);
    expect(state.rows.map((row) => row.kind)).toEqual([
      "user",
      "assistant",
      "user",
      "assistant",
    ]);
  });

  it("does not duplicate rows when the same history replays", () => {
    const once = fold([history]);
    const twice = fold([history], once);
    expect(twice.rows).toHaveLength(once.rows.length);
  });

  it("does not duplicate live rows across a reconnect that replays history", () => {
    const live = fold([
      history,
      { type: "turn_start", turnId: "t1" },
      { type: "text_delta", text: "live answer" },
      { type: "turn_end", status: "ok", tokens: 1, elapsedMs: 1 },
    ]);
    const before = live.rows.length;

    const reconnected = fold([history], live);
    expect(reconnected.rows).toHaveLength(before);
    expect(reconnected.rows.filter((row) => row.kind === "user")).toHaveLength(2);
    expect(reconnected.rows.filter((row) => row.kind === "assistant")).toHaveLength(3);
  });

  it("adds only the messages that arrived while disconnected", () => {
    const live = fold([history, { type: "turn_start", turnId: "t1" }, { type: "text_delta", text: "partial" }]);
    const grown: ServerMessage = {
      ...history,
      messages: [
        ...history.messages,
        { role: "user", content: "third prompt" },
        { role: "assistant", content: "third answer" },
      ],
    };
    const reconnected = fold([grown], live);
    const users = reconnected.rows.filter((row) => row.kind === "user");
    const assistants = reconnected.rows.filter((row) => row.kind === "assistant");
    expect(users).toHaveLength(3);
    // Three from history plus the one live row the client already had.
    expect(assistants).toHaveLength(4);
    expect(reconnected.rows.at(-1)).toMatchObject({ text: "partial" });
  });

  it("keeps live rows below history after reconcile", () => {
    const live = fold([
      { type: "turn_start", turnId: "t1" },
      { type: "text_delta", text: "live" },
    ]);
    const reconnected = fold([history], live);
    const kinds = reconnected.rows.map((row) => row.kind);
    expect(kinds.indexOf("assistant")).toBeLessThan(kinds.lastIndexOf("assistant"));
    expect(reconnected.rows.at(-1)).toMatchObject({ text: "live" });
  });

  it("renders history tool calls and system messages", () => {
    const withTools: ServerMessage = {
      ...history,
      messages: [
        {
          role: "assistant",
          content: "running it",
          tool_calls: [{ id: "h1", function: { name: "ipython", arguments: "print(1)" } }],
        },
        { role: "tool", tool_call_id: "h1", content: "1" },
        { role: "system", content: "kernel restored" },
      ],
    };
    const state = fold([withTools]);
    expect(state.rows.map((row) => row.kind)).toEqual(["assistant", "tool", "bash_notice"]);
    expect(state.rows[1]).toMatchObject({ id: "h1", status: "ok", code: "print(1)" });
  });

  it("adopts turnRunning from the server", () => {
    const running = fold([{ ...history, turnRunning: true } as ServerMessage]);
    expect(running.turnRunning).toBe(true);
  });
});

describe("robustness", () => {
  it("ignores an unknown message type", () => {
    const state = fold([{ type: "future_thing" } as unknown as ServerMessage]);
    expect(state.rows).toHaveLength(0);
  });

  it("never produces duplicate row ids within a turn", () => {
    const state = fold([
      { type: "turn_start", turnId: "t1" },
      { type: "thinking_delta", text: "a" },
      { type: "text_delta", text: "b" },
      { type: "bash_notice", text: "c" },
      { type: "steering", text: "d" },
      { type: "turn_end", status: "ok", tokens: 0, elapsedMs: 0 },
    ]);
    const ids = state.rows.map((row) => row.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("reduces a full scripted turn without throwing", () => {
    const state = fold([
      { type: "turn_start", turnId: "t1" },
      { type: "status", label: "Thinking..." },
      { type: "thinking_delta", text: "hmm" },
      { type: "tool_call", id: "c1", code: "x" },
      { type: "tool_result", id: "c1", ok: true, output: "1", durationMs: 1, images: [], actions: null },
      { type: "text_delta", text: "answer" },
      { type: "bash_notice", text: "job done" },
      { type: "steering", text: "also y" },
      { type: "compaction", summary: "s", messagesBefore: 2, messagesAfter: 1, tokensBefore: 10, tokensAfter: 5 },
      { type: "turn_end", status: "ok", tokens: 5, elapsedMs: 100 },
    ]);
    expect(state.rows.map((row) => row.kind)).toEqual([
      "thinking",
      "tool",
      "assistant",
      "bash_notice",
      "steering",
      "compaction",
      "turn_end",
    ]);
  });
});

describe("helpers", () => {
  it("counts lines", () => {
    expect(lineCount("")).toBe(0);
    expect(lineCount("a")).toBe(1);
    expect(lineCount("a\nb")).toBe(2);
  });

  it("estimates tokens", () => {
    expect(approxTokens("")).toBe(0);
    expect(approxTokens("abcd")).toBe(1);
    expect(approxTokens("abcde")).toBe(2);
  });
});

describe("the local prompt echo", () => {
  it("shows the prompt immediately, before any server frame", () => {
    const state = addUserPrompt(initialState, "  what is 6*7?  ");
    expect(state.rows).toHaveLength(1);
    expect(state.rows[0]).toMatchObject({ kind: "user", text: "what is 6*7?" });
  });

  it("ignores an empty prompt", () => {
    expect(addUserPrompt(initialState, "   ").rows).toHaveLength(0);
  });

  it("keeps the prompt above the turn it started", () => {
    const state = fold(
      [
        { type: "turn_start", turnId: "t1" },
        { type: "text_delta", text: "42" },
      ],
      addUserPrompt(initialState, "what is 6*7?"),
    );
    expect(state.rows.map((row) => row.kind)).toEqual(["user", "assistant"]);
  });

  it("retires the local echo when history replays the same prompt", () => {
    // A reconnect sends `session_state`; the prompt is in both places and must
    // appear exactly once.
    const echoed = addUserPrompt(initialState, "what is 6*7?");
    const state = fold(
      [
        {
          type: "session_state",
          sessionId: "s1",
          cwd: "/tmp",
          model: "m",
          effort: "high",
          turnRunning: false,
          commands: [],
          messages: [
            { role: "user", content: "what is 6*7?" },
            { role: "assistant", content: "42" },
          ],
        },
      ],
      echoed,
    );
    const users = state.rows.filter((row) => row.kind === "user");
    expect(users).toHaveLength(1);
    expect(users[0]?.text).toBe("what is 6*7?");
  });

  it("retires each repeated identical prompt one for one", () => {
    let state = addUserPrompt(initialState, "again");
    state = addUserPrompt(state, "again");
    expect(state.rows.filter((row) => row.kind === "user")).toHaveLength(2);

    state = fold(
      [
        {
          type: "session_state",
          sessionId: "s1",
          cwd: "/tmp",
          model: "m",
          effort: "",
          turnRunning: false,
          commands: [],
          messages: [
            { role: "user", content: "again" },
            { role: "assistant", content: "1" },
            { role: "user", content: "again" },
            { role: "assistant", content: "2" },
          ],
        },
      ],
      state,
    );
    expect(state.rows.filter((row) => row.kind === "user")).toHaveLength(2);
  });

  it("does not retire a local echo that history has not caught up with", () => {
    // History arrives before persistence includes the last prompt (a reconnect
    // mid-turn). The echo must survive, or the prompt vanishes.
    const echoed = addUserPrompt(initialState, "just asked");
    const state = fold(
      [
        {
          type: "session_state",
          sessionId: "s1",
          cwd: "/tmp",
          model: "m",
          effort: "",
          turnRunning: true,
          commands: [],
          messages: [{ role: "user", content: "an older question" }],
        },
      ],
      echoed,
    );
    const texts = state.rows.filter((row) => row.kind === "user").map((row) => row.text);
    expect(texts).toContain("just asked");
    expect(texts).toContain("an older question");
  });
});
