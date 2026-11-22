import { describe, expect, it, beforeEach } from "vitest";
import {
  addUserPrompt,
  approxTokens,
  failedToolCount,
  groupTurns,
  initialState,
  isTurn,
  lastUserRowId,
  lineCount,
  parseHarnessNotice,
  reduceAll,
  resetIdCounter,
  sameConversation,
  setRowFormState,
  stepLabel,
  trayOwnsStatus,
  turnSummary,
  type AssistantRow,
  type Row,
  type ThinkingRow,
  type ToolRow,
  type TranscriptItem,
  type TranscriptState,
  type Turn,
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

  it("classifies a harness notice as a notice, not as the reader speaking", () => {
    // A resumed session opens with this: the application reporting that it
    // restored a kernel, delivered on the only role the wire lets it use.
    const notice =
      "[python-state-restored]\nRestored 2 Python variables/functions from the previous session: df, model.\nNothing was skipped.";
    const state = fold([
      {
        type: "session_state",
        sessionId: "s1",
        cwd: "/tmp",
        model: "m",
        effort: "high",
        turnRunning: false,
        commands: [],
        messages: [{ role: "user", content: notice }],
      },
    ]);

    expect(state.rows).toHaveLength(1);
    expect(state.rows[0]).toMatchObject({
      kind: "system_notice",
      tag: "python-state-restored",
      text: "Restored 2 Python variables/functions from the previous session: df, model.\nNothing was skipped.",
    });
    // Nothing claims the reader said it.
    expect(state.rows.some((row) => row.kind === "user")).toBe(false);
  });

  it("keeps the reader's own bracketed words out of the notice rule", () => {
    const state = fold([
      {
        type: "session_state",
        sessionId: "s1",
        cwd: "/tmp",
        model: "m",
        effort: "",
        turnRunning: false,
        commands: [],
        messages: [
          { role: "user", content: "[x] try this instead" },
          { role: "user", content: "look at [the docs] please" },
        ],
      },
    ]);
    expect(state.rows.map((row) => row.kind)).toEqual(["user", "user"]);
  });
});

describe("harness notices", () => {
  it("splits the tag from the body it announced", () => {
    expect(
      parseHarnessNotice("[bash-done pid:4821 exit:0 duration:1.2s]\ncommand: ls\ntail:\nout"),
    ).toEqual({
      tag: "bash-done pid:4821 exit:0 duration:1.2s",
      text: "command: ls\ntail:\nout",
    });
  });

  it("reads a background status notice", () => {
    // A running job crossing the check period: same shape as a done notice, so a
    // status must land in the transcript's notice column, not a user bubble.
    expect(
      parseHarnessNotice("[bash-status pid:4821 elapsed:5m02s]\ncommand: make\ntail:\nbuilding"),
    ).toEqual({
      tag: "bash-status pid:4821 elapsed:5m02s",
      text: "command: make\ntail:\nbuilding",
    });
  });

  it("reads a one-line notice's sentence after its tag", () => {
    expect(parseHarnessNotice("[progress] Update the user now on the progress.")).toEqual({
      tag: "progress",
      text: "Update the user now on the progress.",
    });
  });

  it("reads ordinary prose as ordinary prose", () => {
    expect(parseHarnessNotice("what is 6*7?")).toBeNull();
    // Brackets stop being a tag the moment they are not the first thing said.
    expect(parseHarnessNotice("please [python-state-restored] this")).toBeNull();
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

  it("retires the previous session's rows when the session id changes", () => {
    // Switching sessions from the sidebar replays `session_state` for another log,
    // whose history rows carry the same positional ids. Merging would silently drop
    // the first message of the new session and leave the old answer on screen.
    const other: ServerMessage = {
      ...history,
      sessionId: "s-2",
      cwd: "/elsewhere",
      messages: [
        { role: "user", content: "other prompt" },
        { role: "assistant", content: "other answer" },
      ],
    };

    const state = fold([history, other]);

    expect(state.session).toMatchObject({ sessionId: "s-2", cwd: "/elsewhere" });
    expect(state.rows.map((row) => row.kind)).toEqual(["user", "assistant"]);
    expect(state.rows[0]).toMatchObject({ kind: "user", text: "other prompt" });
    expect(state.rows[1]).toMatchObject({ kind: "assistant", text: "other answer" });
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

  it("does not duplicate assistant rows when session_state replays after a finished turn (e.g. Rich UI toggle)", () => {
    const live = fold([
      history,
      { type: "turn_start", turnId: "t1" },
      { type: "text_delta", text: "root = Stack([])" },
      { type: "turn_end", status: "ok", tokens: 1, elapsedMs: 1 },
    ]);
    const liveAssistant = live.rows.find(
      (row): row is AssistantRow => row.kind === "assistant" && row.text === "root = Stack([])",
    );
    expect(liveAssistant).toBeDefined();
    const withForm = setRowFormState(live, liveAssistant!.id, { field: "saved" });

    const afterToggle = fold(
      [
        {
          ...history,
          messages: [
            ...history.messages,
            { role: "user", content: "draw UI" },
            { role: "assistant", content: "root = Stack([])" },
          ],
        },
      ],
      withForm,
    );

    const assistants = afterToggle.rows.filter((row) => row.kind === "assistant");
    expect(assistants).toHaveLength(3);
    expect(assistants[2]).toMatchObject({
      text: "root = Stack([])",
      formState: { field: "saved" },
    });
  });

  it("does not split the live assistant row on mid-turn reconnect (session_state -> turn_start -> text_delta)", () => {
    const reconnected = fold([
      {
        ...history,
        turnRunning: true,
        streamingText: "Hello ",
      },
      { type: "turn_start", turnId: "t-reattach" },
      { type: "text_delta", text: "world" },
    ]);

    const assistants = reconnected.rows.filter((row) => row.kind === "assistant");
    expect(assistants).toHaveLength(3);
    expect(assistants[2]).toMatchObject({ text: "Hello world" });
  });

  it("preserves multimodal user prompts and tool outputs on history replay", () => {
    const state = fold([
      {
        ...history,
        messages: [
          {
            role: "user",
            content: [
              { type: "text", text: "what is in this image?" },
              { type: "image_url", image_url: { url: "data:image/png;base64,AAAA" } },
            ],
          },
          {
            role: "assistant",
            content: "let me inspect",
            tool_calls: [
              { id: "tc-json", function: { name: "ipython", arguments: '{"code":"x = 42\\nprint(x)"}' } },
            ],
          },
          { role: "tool", tool_call_id: "tc-json", content: "42\n" },
        ],
      },
    ]);

    expect(state.rows[0]).toMatchObject({
      kind: "user",
      text: "what is in this image?",
      images: [{ mimeType: "image/png", data: "AAAA" }],
    });
    expect(state.rows[2]).toMatchObject({
      kind: "tool",
      id: "tc-json",
      code: "x = 42\nprint(x)",
      status: "ok",
      output: "42\n",
    });
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

describe("OpenUI form state on a row", () => {
  const streamed = (): TranscriptState =>
    fold([
      { type: "turn_start", turnId: "t1" },
      { type: "text_delta", text: "root = Stack([])" },
      { type: "turn_end", status: "ok", tokens: 1, elapsedMs: 1 },
    ]);

  it("starts null, so markdown mode never sees form state", () => {
    const row = streamed().rows.find((entry) => entry.kind === "assistant");
    expect(row).toMatchObject({ formState: null });
  });

  const assistantOf = (state: TranscriptState): { id: string; text: string } => {
    const row = state.rows.find((entry) => entry.kind === "assistant");
    if (row === undefined || row.kind !== "assistant") throw new Error("no assistant row");
    return row;
  };

  it("attaches state to the row that rendered it", () => {
    const before = streamed();
    const { id } = assistantOf(before);
    const after = setRowFormState(before, id, { mock_config_form: { notes: "hi" } });

    expect(assistantOf(after)).toMatchObject({
      formState: { mock_config_form: { notes: "hi" } },
    });
    // The text is untouched: this is not encoded back into the message.
    expect(assistantOf(after).text).toBe("root = Stack([])");
  });

  it("ignores an unknown id or a non-assistant row", () => {
    const state = fold([{ type: "turn_start", turnId: "t1" }, { type: "text_delta", text: "x" }]);
    expect(setRowFormState(state, "nope", { a: 1 })).toBe(state);

    const toolRow = state.rows.find((entry) => entry.kind === "turn_end")?.id ?? "";
    expect(setRowFormState(state, toolRow, { a: 1 })).toBe(state);
  });

  it("survives later deltas to other rows", () => {
    const before = streamed();
    const { id } = assistantOf(before);
    const withState = setRowFormState(before, id, { f: 1 });
    const more = reduceAll(withState, [
      { type: "turn_start", turnId: "t2" },
      { type: "text_delta", text: "second" },
    ]);
    expect(assistantOf(more)).toMatchObject({ formState: { f: 1 } });
  });
});
/* -------------------------------------------------------------- turn grouping */

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

const user = (id: string, text: string): Row => ({ kind: "user", id, text, images: [] });

const turnEnd = (id: string): Row => ({ kind: "turn_end", id, status: "ok", tokens: 1, elapsedMs: 10 });

/** The turns among the grouped items, in order. */
const turnsOf = (items: TranscriptItem[]): Turn[] => items.filter(isTurn);

const stepKinds = (turn: Turn): string[] => turn.steps.map((step) => step.kind);

describe("groupTurns", () => {
  it("promotes the last narrated assistant row to the answer", () => {
    const [turn] = turnsOf(
      groupTurns([
        thinking("k1", "consider"),
        assistant("a1", "Let me check the data."),
        tool("t1"),
        assistant("a2", "The mean is 4."),
      ]),
    );

    // The reply is the final block, not the narration that introduced the tool.
    expect(turn?.answer).toMatchObject({ id: "a2", text: "The mean is 4." });
    expect(stepKinds(turn!)).toEqual(["thinking", "text", "tool_run"]);
    expect(turn?.steps.some((step) => step.kind === "text" && step.row.id === "a2")).toBe(false);
  });

  it("never promotes an assistant row with no text", () => {
    const [turn] = turnsOf(
      groupTurns([assistant("a1", "the answer"), assistant("a2", "   "), tool("t1")]),
    );

    expect(turn?.answer).toMatchObject({ id: "a1" });
    // An empty assistant row is a tool-only segment; it stays in the steps.
    expect(stepKinds(turn!)).toEqual(["text", "tool_run"]);
  });

  it("has no answer when a turn never produced text", () => {
    const [turn] = turnsOf(groupTurns([thinking("k1", "hmm"), tool("t1")]));
    expect(turn?.answer).toBeNull();
  });

  it("folds consecutive tool rows into one run and keeps the run order", () => {
    const [turn] = turnsOf(
      groupTurns([tool("t1"), tool("t2"), tool("t3"), assistant("a1", "done")]),
    );

    const steps = turn!.steps;
    expect(steps).toHaveLength(1);
    expect(steps[0]).toMatchObject({ kind: "tool_run", id: "tool-run-t1" });
    expect(steps[0]?.kind === "tool_run" ? steps[0].rows.map((row) => row.id) : []).toEqual([
      "t1",
      "t2",
      "t3",
    ]);
  });

  it("splits tool runs around the narration between them", () => {
    const [turn] = turnsOf(
      groupTurns([tool("t1"), assistant("a1", "one down"), tool("t2"), assistant("a2", "both done")]),
    );
    expect(stepKinds(turn!)).toEqual(["tool_run", "text", "tool_run"]);
    expect(turn?.answer).toMatchObject({ id: "a2" });
  });

  it("aggregates consecutive thinking rows into one step", () => {
    const [turn] = turnsOf(
      groupTurns([thinking("k1", "a"), thinking("k2", "b"), tool("t1")]),
    );
    const [first] = turn!.steps;
    expect(first).toMatchObject({ kind: "thinking", id: "thinking-k1" });
    expect(first?.kind === "thinking" ? first.rows.map((row) => row.id) : []).toEqual(["k1", "k2"]);
  });

  it("keeps step ids stable as later rows stream in", () => {
    const before = turnsOf(groupTurns([thinking("k1", "a"), tool("t1")]))[0];
    const after = turnsOf(groupTurns([thinking("k1", "a"), tool("t1"), tool("t2")]))[0];

    // Ids come from the first row of a run, so the second tool call extends the
    // same disclosure instead of remounting - and collapsing - it.
    expect(after!.steps.map((step) => step.id)).toEqual(before!.steps.map((step) => step.id));
  });

  it("returns every other row kind standalone and in its original position", () => {
    const rows: Row[] = [
      user("u1", "hi"),
      assistant("a1", "first answer"),
      turnEnd("te1"),
      user("u2", "again"),
      thinking("k1", "hmm"),
    ];
    const items = groupTurns(rows, { running: true });

    expect(items.map((item) => (isTurn(item) ? `turn ${item.id}` : item.id))).toEqual([
      "u1",
      "turn turn-a1",
      "te1",
      "u2",
      "turn turn-k1",
    ]);
  });

  it("marks only the trailing turn live, and only while turnRunning", () => {
    const rows: Row[] = [assistant("a1", "one"), turnEnd("te1"), assistant("a2", "two")];

    expect(turnsOf(groupTurns(rows, { running: true })).map((turn) => turn.live)).toEqual([
      false,
      true,
    ]);
    // Settled: the caller says nothing is running.
    expect(turnsOf(groupTurns(rows, { running: false })).map((turn) => turn.live)).toEqual([
      false,
      false,
    ]);
  });

  it("does not call a trailing turn live when a later marker closed it", () => {
    const items = groupTurns([assistant("a1", "one"), turnEnd("te1")], { running: true });
    expect(turnsOf(items).map((turn) => turn.live)).toEqual([false]);
  });

  it("keeps a standalone row between turns from merging two turns into one", () => {
    const items = groupTurns([
      assistant("a1", "one"),
      { kind: "error", id: "e1", message: "boom", fatal: false } as Row,
      assistant("a2", "two"),
    ]);
    expect(turnsOf(items)).toHaveLength(2);
  });
});

describe("turn summaries", () => {
  const fold = (...rows: Row[]): Turn => turnsOf(groupTurns(rows))[0]!;

  it("counts failures across every tool run in the turn", () => {
    const turn = fold(
      tool("t1"),
      tool("t2", "failed"),
      assistant("a1", "narration"),
      tool("t3", "failed"),
      assistant("a2", "answer"),
    );
    expect(failedToolCount(turn)).toBe(2);
  });

  it("reports zero failures for a clean turn", () => {
    expect(failedToolCount(fold(tool("t1"), tool("t2")))).toBe(0);
  });

  it("labels a step by what it is doing, not by how many it is", () => {
    expect(stepLabel({ kind: "thinking", id: "thinking-k1", rows: [thinking("k1", "a")] })).toBe(
      "Thinking",
    );
    expect(stepLabel({ kind: "text", id: "text-a1", row: assistant("a1", "Looking at it") })).toBe(
      "Looking at it",
    );
    expect(stepLabel({ kind: "text", id: "text-a1", row: assistant("a1", "\n  ") })).toBe("Reply");
  });

  it("leads with the running tool's code while the run is live", () => {
    const turn = fold(tool("t1", "running", "read_csv('data.csv')"), assistant("a1", "done"));
    expect(stepLabel(turn.steps[0]!)).toBe("Running read_csv");
  });

  it("falls back to a count once nothing in the run is running", () => {
    const turn = fold(tool("t1"), tool("t2"), tool("t3"), assistant("a1", "done"));
    expect(stepLabel(turn.steps[0]!)).toBe("Used 3 tools");
  });

  it("names a non-call cell by its line, clipped to one", () => {
    const long = "import pandas as pd, numpy as np, and a great many other things besides";
    const turn = fold(tool("t1", "running", long), assistant("a1", "done"));
    const label = stepLabel(turn.steps[0]!);

    expect(label.startsWith("Running import pandas as pd")).toBe(true);
    expect(label).toContain("…");
    expect(label).not.toContain("\n");
  });

  it("names a call by the function, not the assignment or the module", () => {
    const turn = fold(
      tool("t1", "running", "df = pd.read_csv('x')\ndf.head()"),
      assistant("a1", "done"),
    );
    expect(stepLabel(turn.steps[0]!)).toBe("Running read_csv");
  });

  it("says 'a tool' when a running call has no code yet", () => {
    const turn = fold(tool("t1", "running", ""), assistant("a1", "done"));
    expect(stepLabel(turn.steps[0]!)).toBe("Running a tool");
  });
});

describe("the settled header", () => {
  const turnOf = (...rows: Row[]): Turn => turnsOf(groupTurns(rows))[0]!;
  const manyTools = (count: number): Row[] =>
    Array.from({ length: count }, (_, index) => tool(`t${index + 1}`));

  it("names the work, not how many rows the tray holds", () => {
    // The old header said "1 step" over a body reading "Used 4 tools": one idea,
    // two units, and the useless number in front.
    const turn = turnOf(...manyTools(4), assistant("a1", "done"));
    expect(turn.steps).toHaveLength(1);
    expect(turnSummary(turn)).toBe("4 tools");
  });

  it("says so when there was also thinking, still with one count", () => {
    const turn = turnOf(thinking("k1", "hmm"), ...manyTools(4), assistant("a1", "done"));
    expect(turnSummary(turn)).toBe("Thought, 4 tools");
  });

  it("never counts steps alongside the tools inside them", () => {
    const turn = turnOf(
      thinking("k1", "a"),
      tool("t1"),
      assistant("a1", "one down"),
      tool("t2"),
      tool("t3"),
      assistant("a2", "answer"),
    );
    // Three steps, three tools: the header says the second one only.
    expect(turn.steps).toHaveLength(4);
    expect(turnSummary(turn)).toBe("Thought, 3 tools");
  });

  it("says 'tool' for one", () => {
    expect(turnSummary(turnOf(tool("t1"), assistant("a1", "done")))).toBe("1 tool");
  });

  it("says only 'Thought' when nothing ran", () => {
    expect(turnSummary(turnOf(thinking("k1", "a"), assistant("a1", "done")))).toBe("Thought");
  });

  it("leaves the failure count to its own suffix", () => {
    const turn = turnOf(tool("t1", "failed"), tool("t2", "failed"), tool("t3"), assistant("a1", "x"));
    // The header reads "3 tools · 2 failed": the count of what ran, the count of
    // what broke, and nothing counted twice.
    expect(turnSummary(turn)).toBe("3 tools");
    expect(failedToolCount(turn)).toBe(2);
  });
});

describe("who owns a running turn", () => {
  it("hands the tray's own words to the tray while a turn runs", () => {
    // The tray header is the live line: "Thinking", "Running read_csv", "Writing a
    // reply". The server's label for the same moment is not a second line.
    expect(trayOwnsStatus("Thinking...", true)).toBe(true);
    expect(trayOwnsStatus("Running ipython...", true)).toBe(true);
    expect(trayOwnsStatus("Writing a reply", true)).toBe(true);
  });

  it("keeps a label the tray cannot know", () => {
    expect(trayOwnsStatus("Compacting context…", true)).toBe(false);
  });

  it("never hides a label when no turn runs, because no tray is speaking", () => {
    expect(trayOwnsStatus("Thinking...", false)).toBe(false);
    expect(trayOwnsStatus(null, true)).toBe(false);
  });
});

describe("following the stream", () => {
  it("finds the newest row the reader spoke", () => {
    const rows: Row[] = [user("u1", "first"), assistant("a1", "one"), user("u2", "second")];
    expect(lastUserRowId(rows)).toBe("u2");
    expect(lastUserRowId([assistant("a1", "one")])).toBeNull();
    expect(lastUserRowId([])).toBeNull();
  });

  it("treats growth as the same conversation and a replacement as another one", () => {
    const first: Row[] = [user("u1", "first"), assistant("a1", "one")];
    const grown: Row[] = [...first, assistant("a2", "more")];
    expect(sameConversation(first, grown)).toBe(true);

    // History rows are numbered by position, so two sessions reuse "history-user-0".
    // The words are what tells them apart.
    const sessionOne: Row[] = [
      { kind: "user", id: "history-user-0", text: "about the data", images: [] },
      { kind: "assistant", id: "history-assistant-1", text: "here it is", formState: null },
    ];
    const sessionTwo: Row[] = [
      { kind: "user", id: "history-user-0", text: "about the plots", images: [] },
      { kind: "assistant", id: "history-assistant-1", text: "here they are", formState: null },
    ];
    expect(sameConversation(sessionOne, sessionTwo)).toBe(false);
  });

  it("does not call the first render a switch", () => {
    expect(sameConversation([], [user("u1", "hi")])).toBe(true);
  });

  it("does call an emptied log one, because that is a new session", () => {
    expect(sameConversation([user("u1", "hi")], [])).toBe(false);
  });
});

describe("a reconnect in the middle of a turn", () => {
  /**
   * The bug this covers: a phone's screen turns off, the socket dies, and the page
   * comes back while the answer is still being written. History stops at the prompt
   * (the loop commits an assistant message only when a step ends), so the reconnected
   * page showed the question and then nothing -- "the chat no longer updates".
   */
  it("draws the text the running turn has already written", () => {
    const state = fold([
      {
        type: "session_state",
        sessionId: "s",
        cwd: "/r",
        model: "m",
        effort: "e",
        turnRunning: true,
        commands: [],
        messages: [{ role: "user", content: "tell me about looms" }],
        streamingText: "The loom is one of the oldest",
      },
    ]);
    const assistant = state.rows.filter((row) => row.kind === "assistant");
    expect(assistant.map((row) => row.text)).toEqual(["The loom is one of the oldest"]);
    expect(state.turnRunning).toBe(true);
  });

  it("keeps appending live deltas to the text it reconnected with", () => {
    const state = fold([
      {
        type: "session_state",
        sessionId: "s",
        cwd: "/r",
        model: "m",
        effort: "e",
        turnRunning: true,
        commands: [],
        messages: [{ role: "user", content: "q" }],
        streamingText: "The loom is",
      },
      { type: "text_delta", text: " one of the oldest tools." },
    ]);
    const assistant = state.rows.filter((row) => row.kind === "assistant");
    expect(assistant.map((row) => row.text)).toEqual(["The loom is one of the oldest tools."]);
  });

  it("does not duplicate text history already carries", () => {
    // The step committed while the socket was down: the snapshot has both the
    // message and the live text of the next step starting from scratch.
    const state = fold([
      {
        type: "session_state",
        sessionId: "s",
        cwd: "/r",
        model: "m",
        effort: "e",
        turnRunning: true,
        commands: [],
        messages: [
          { role: "user", content: "q" },
          { role: "assistant", content: "First paragraph." },
        ],
        streamingText: "First paragraph.",
      },
    ]);
    const assistant = state.rows.filter((row) => row.kind === "assistant");
    expect(assistant.map((row) => row.text)).toEqual(["First paragraph."]);
  });

  it("ignores an absent or empty field, so an older server still works", () => {
    const state = fold([
      {
        type: "session_state",
        sessionId: "s",
        cwd: "/r",
        model: "m",
        effort: "e",
        turnRunning: false,
        commands: [],
        messages: [{ role: "user", content: "q" }],
      },
    ]);
    expect(state.rows.filter((row) => row.kind === "assistant")).toHaveLength(0);
  });

  it("does not touch a transcript when no turn is running", () => {
    const state = fold([
      {
        type: "session_state",
        sessionId: "s",
        cwd: "/r",
        model: "m",
        effort: "e",
        turnRunning: false,
        commands: [],
        messages: [{ role: "user", content: "q" }],
        streamingText: "",
      },
    ]);
    expect(state.rows.filter((row) => row.kind === "assistant")).toHaveLength(0);
  });

  it("stores projectTrust from session_state onto session metadata", () => {
    const state = fold([
      {
        type: "session_state",
        sessionId: "s",
        cwd: "/repo",
        model: "m",
        effort: "e",
        turnRunning: false,
        commands: [],
        messages: [],
        projectTrust: {
          cwd: "/repo",
          trusted: false,
          files: [".quartz/helpers.py"],
          needsTrust: true,
        },
      },
    ]);
    expect(state.session.projectTrust).toEqual({
      cwd: "/repo",
      trusted: false,
      files: [".quartz/helpers.py"],
      needsTrust: true,
    });
  });
});
