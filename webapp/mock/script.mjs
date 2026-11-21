/**
 * The scripted session the mock replays, kept free of any server code so tests can
 * fold exactly the frames a developer sees in the browser.
 *
 * `docs/web-protocol.md` is the contract these frames obey.
 */

/* A 1x1 PNG, so the image path renders without shipping a binary fixture. */
export const TINY_PNG =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==";

export const COMMANDS = [
  { name: "model", description: "Choose provider, model, and reasoning effort", usage: "[prov]" },
  { name: "settings", description: "Show or change compaction and progress settings", usage: "[key val]" },
  { name: "compact", description: "Compact the conversation now", usage: "[tokens]" },
  { name: "jobs", description: "List background bash jobs" },
  { name: "vars", description: "List kernel variables" },
  { name: "helpers", description: "List quartz helper functions" },
  { name: "usage", description: "Show token and cost usage for this session" },
  { name: "expand", description: "Show the full output of an earlier cell", usage: "[n]" },
  { name: "session", description: "Switch the active session", usage: "[id]" },
  { name: "continue", description: "Resume the most recent session" },
  { name: "login", description: "Log in to a provider (OAuth or API key)", usage: "[prov]" },
  { name: "logout", description: "Log out and remove stored provider credentials", usage: "[prov]" },
  { name: "image", description: "Attach an image file to the next prompt", usage: "<path>" },
  { name: "paste", description: "Attach an image from the clipboard" },
  { name: "prompt", description: "Show the system prompt Quartz is running" },
  { name: "reset", description: "Reset the kernel to a clean namespace" },
  { name: "help", description: "Show available slash commands" },
  { name: "exit", description: "Exit quartz" },
];

export const sessionState = () => ({
  type: "session_state",
  sessionId: "20261121-0930-mock-session-4f2a91c3",
  cwd: "/home/danielwijaya/unharness-explore",
  model: "claude-sonnet-4-5",
  effort: "medium",
  turnRunning: false,
  commands: COMMANDS,
  messages: [
    { role: "user", content: "What does the kernel client do on start?" },
    {
      role: "assistant",
      content:
        "It spawns the worker process and wires up the pipes.\n\n" +
        "`KernelClient.start()` does three things: boot the worker, wait for the ready line, " +
        "and hold the write lock open so the first cell cannot race the handshake.",
      tool_calls: [{ id: "hist-call-1", type: "function", function: { name: "ipython", arguments: "print(kernel.pid)" } }],
    },
    { role: "tool", tool_call_id: "hist-call-1", content: "4242" },
  ],
});

/* ------------------------------------------------------------------ script */

export function scriptedTurn() {
  const frames = [];
  const push = (frame) => frames.push(frame);

  push({ type: "turn_start", turnId: "turn-mock-1" });
  push({ type: "status", label: "Thinking..." });

  // Thinking first: the model reasons, then narrates, then acts.
  const thinking = [
    "The user wants to know how the kernel client boots. ",
    "I should read the client and the worker to be precise. ",
    "Let me check `start()` and the ready handshake. ",
    "Then I can explain it with a short example.",
  ];
  for (const chunk of thinking) push({ type: "thinking_delta", text: chunk });

  push({ type: "status", label: "Reading kernel/client.py..." });
  push({ type: "tool_call", id: "call-read-1", code: "open('src/quartz/kernel/client.py').read()[:400]" });
  push({
    type: "tool_result",
    id: "call-read-1",
    ok: true,
    durationMs: 12,
    output:
      'class KernelClient:\n    """Owns the worker subprocess and the pipe protocol."""\n\n    def start(self) -> None:\n        self._proc = Popen(...)\n        self._await_ready()',
    images: [],
    actions: null,
  });

  push({ type: "status", label: "Thinking..." });
  const prose = [
    "## How the kernel boots\n\n",
    "`KernelClient.start()` is a three-step handshake:\n\n",
    "1. **Spawn** the worker with `Popen`, inheriting the cwd.\n",
    "2. **Await ready** — the worker writes one line once the REPL is live, so the first ",
    "cell can never race the import.\n",
    "3. **Hold the write lock** across both, which is why a burst of cells queues ",
    "instead of interleaving.\n\n",
    "The payoff is that the kernel outlives any single turn:\n\n",
    "```python\n",
    "kernel = KernelClient(cwd=Path.cwd())\n",
    "kernel.start()          # blocks until the worker is ready\n",
    "result = kernel.run_cell('x = 41 + 1')\n",
    "assert result.ok\n",
    "```\n\n",
    "That is the whole contract — the rest of the module is the pipe protocol on top of it.",
  ];
  for (const chunk of prose) push({ type: "text_delta", text: chunk });

  push({ type: "status", label: "Running cell..." });
  push({ type: "tool_call", id: "call-run-2", code: "print(x * 2)" });
  push({
    type: "tool_result",
    id: "call-run-2",
    ok: true,
    durationMs: 341,
    output: "84",
    images: [],
    actions: null,
  });

  // A background job finishes mid-turn.
  push({ type: "bash_notice", text: "bash job 3 finished (exit 0) — pytest -q · 469 passed in 36.7s" });

  // The user steers, and the model acknowledges in-stream.
  push({ type: "steering", text: "also show the image output path" });
  push({ type: "status", label: "Checking image output..." });

  // A failing cell: the reducer must render failure, not swallow it.
  push({ type: "tool_call", id: "call-fail-3", code: "open('/nonexistent/path').read()" });
  push({
    type: "tool_result",
    id: "call-fail-3",
    ok: false,
    durationMs: 3,
    output: "FileNotFoundError: [Errno 2] No such file or directory: '/nonexistent/path'",
    images: [],
    actions: null,
  });

  // A successful cell carrying an image and side-effect actions.
  push({ type: "tool_call", id: "call-img-4", code: "fig.savefig('plot.png'); fig" });
  push({
    type: "tool_result",
    id: "call-img-4",
    ok: true,
    durationMs: 128,
    output: "Figure(640x480)",
    images: [{ mimeType: "image/png", data: TINY_PNG }],
    actions: { wrote: "plot.png", cells: 4 },
  });

  // Compaction boundary, then the turn closes.
  push({
    type: "compaction",
    summary: "Summarised the first eleven cells; kept the last two turns verbatim.",
    messagesBefore: 28,
    messagesAfter: 9,
    tokensBefore: 143_812,
    tokensAfter: 41_207,
  });

  push({
    type: "turn_end",
    status: "ok",
    tokens: 1_284,
    elapsedMs: 18_430,
  });

  return frames;
}

export function commandResultFrame(name) {
  switch (name) {
    case "usage":
      return {
        type: "command_result",
        name,
        ok: true,
        result: {
          kind: "text",
          title: "Usage",
          body: "**1,284** tokens this turn · **41,207** in context · 2 turns.\n\n| where | tokens |\n| --- | --- |\n| prompt | 1,102 |\n| completion | 182 |",
        },
      };
    case "jobs":
      return {
        type: "command_result",
        name,
        ok: true,
        result: {
          kind: "list",
          title: "Background jobs",
          items: [
            { label: "3", detail: "exit 0 · pytest -q" },
            { label: "2", detail: "running · npm run dev" },
            { label: "1", detail: "killed · uv build" },
          ],
        },
      };
    case "model":
    case "login":
    case "session":
    case "continue":
      return {
        type: "command_result",
        name,
        ok: true,
        result: {
          kind: "needs_terminal",
          message: `/${name} opens an interactive picker, which the web transport cannot render yet.`,
        },
      };
    case "nope":
      return {
        type: "command_result",
        name,
        ok: false,
        result: { kind: "error", message: `Unknown command: /${name}` },
      };
    default:
      return {
        type: "command_result",
        name,
        ok: true,
        result: {
          kind: "text",
          title: `/${name}`,
          body: `The mock has no special case for \`/${name}\`, so here is the generic reply. Any \`CommandResult\` kind renders here.`,
        },
      };
  }
}

