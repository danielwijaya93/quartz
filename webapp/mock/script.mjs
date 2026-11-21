/**
 * The scripted session the mock replays, kept free of any server code so tests can
 * fold exactly the frames a developer sees in the browser.
 *
 * `docs/web-protocol.md` is the contract these frames obey.
 */

/**
 * A 320x200 PNG that reads as a real matplotlib figure — titled "SIGNAL", axes,
 * ticks, and a decaying oscillation. It is inlined so the fixture needs no binary
 * file and no network at runtime.
 *
 * It replaces the original 1x1 transparent pixel: that proved the image path
 * worked but rendered at ~3x3 CSS px, so image output was effectively invisible
 * during review. This size also exercises the row's `max-h-56` clamp, which a
 * pixel could never do.
 *
 * Generated once with a throwaway zlib-based writer and a 5x7 bitmap font; the
 * committed artifact is only this string.
 */
export const PLOT_PNG =
  "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";

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

/**
 * A canned openui-lang reply: a KPI row, a bar chart and a table, plus an
 * interactive form whose submit fires `@ToAssistant`.
 *
 * The mock replays this instead of the markdown script when the client's `hello`
 * carried a `systemPromptAppend`, which is exactly the condition under which the
 * real model is told to emit openui-lang. So the Rich UI toggle exercises the
 * whole path -- prompting decision, renderer, form round-trip -- with no API key.
 */
export const OPENUI_TURN_LANG = [
  "root = Stack([header, kpis, chartCard, tableCard, formCard])",
  'header = CardHeader("Kernel activity, last 7 days", "Replayed by the Quartz mock")',
  "kpis = SnippetCardBlock([kpiCells, kpiJobs])",
  'kpiCells = SnippetCardItem("cells", cellsLabel, cellsValue)',
  'cellsIcon = Icon("cpu", "development")',
  'cellsLabel = IconText(cellsIcon, "neutral", "m", "Cells run", "Across 12 sessions", false, "horizontal")',
  'cellsValue = BoldText("number", "1,284", "+18% vs last week")',
  'kpiJobs = SnippetCardItem("jobs", jobsLabel, jobsValue)',
  'jobsIcon = Icon("terminal", "development")',
  'jobsLabel = IconText(jobsIcon, "neutral", "m", "Bash jobs", "pytest, ruff, uv", false, "horizontal")',
  'jobsValue = BoldText("number", "37", "all exits 0")',
  'chartCard = Card([chartHeader, runsChart], "card")',
  'chartHeader = CardHeader("Cells per day", "Bar height is the number of cells")',
  'runsChart = BarChart(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], [runsSeries], "grouped", "Day", "Cells")',
  'runsSeries = Series("Cells", [142, 219, 168, 265, 301, 89, 100])',
  'tableCard = Card([tableHeader, runsTable], "card")',
  'tableHeader = CardHeader("Slowest cells", "Wall-clock duration, milliseconds")',
  "runsTable = Table([cellCol, msCol])",
  'cellCol = Col("Cell", ["fig.savefig(plot.png)", "print(x * 2)", "kernel.run_cell(y)"], "string")',
  'msCol = Col("Duration", [128, 341, 902], "number")',
  'formCard = Card([formHeader, configForm], "card")',
  'formHeader = CardHeader("Tune the next turn", "Submit to send your choices back to Quartz")',
  'effortSelect = Select("effort", [effortLow, effortMed, effortHigh], "Choose effort", null, $effort)',
  'effortLow = SelectItem("low", "Low")',
  'effortMed = SelectItem("medium", "Medium")',
  'effortHigh = SelectItem("high", "High")',
  'effortField = FormControl("Reasoning effort", effortSelect, "How long Quartz may think")',
  'cellSlider = Slider("cells", "discrete", 1, 20, 1, [5], "Cells to run")',
  'cellField = FormControl("Cells to run", cellSlider)',
  'notesArea = TextArea("notes", "Anything else Quartz should know?", 2)',
  'notesField = FormControl("Notes", notesArea, "Optional")',
  'submitBtn = Button("Apply and run", @ToAssistant("Apply these settings"), "primary")',
  "formButtons = Buttons([submitBtn])",
  'configForm = Form("mock_config_form", formButtons, [effortField, cellField, notesField])',
].join("\n");

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

  // A recoverable error mid-turn: rendered as a danger callout, not a stopped
  // session, so the turn carries on.
  push({ type: "error", message: "Provider stream stalled for 12s; resumed automatically.", fatal: false });

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
    images: [{ mimeType: "image/png", data: PLOT_PNG }],
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

/**
 * The openui-lang variant of `scriptedTurn`: same turn bracket and the same
 * tool activity, but the reply is a surface instead of prose.
 */
export function scriptedOpenUITurn() {
  const frames = [];
  const push = (frame) => frames.push(frame);

  push({ type: "turn_start", turnId: "turn-mock-openui" });
  push({ type: "status", label: "Reading kernel history..." });
  push({ type: "tool_call", id: "call-openui-1", code: "kernel.list_history()[-7:]" });
  push({
    type: "tool_result",
    id: "call-openui-1",
    ok: true,
    durationMs: 41,
    output: "7 days, 1,284 cells",
    images: [],
    actions: null,
  });

  // Delivered in two chunks so the client renders a *partial* tree first: that is
  // the streaming path, and it is the one most likely to break silently.
  const half = Math.floor(OPENUI_TURN_LANG.length / 2);
  push({ type: "text_delta", text: OPENUI_TURN_LANG.slice(0, half) + "\n" });
  push({ type: "text_delta", text: OPENUI_TURN_LANG.slice(half) });

  push({ type: "turn_end", status: "ok", tokens: 2_140, elapsedMs: 9_120 });
  return frames;
}

/**
 * Verbose help, exactly as `CommandRegistry.format_help()` pads it: two columns,
 * then a keybinding block. It is `kind: "text"`, and it is why the client has to
 * recognise columnar text - markdown would flatten it into one paragraph.
 */
const HELP_BODY = [
  "/compact         Compact the conversation now",
  "/expand [n]      Show the full output of an earlier cell",
  "/help            Show available slash commands",
  "/helpers         List quartz helper functions",
  "/image <path>    Attach an image file to the next prompt",
  "/jobs            List background bash jobs",
  "/model [prov]    Choose provider, model, and reasoning effort",
  "/settings [key val]Show or change compaction settings",
  "/usage           Show token and cost usage",
  "",
  "Keybindings:",
  "  Enter            Submit prompt or steer running turn",
  "  Ctrl+J / \\+Enter Insert newline",
  "  Esc              Abort running turn or cancel modal/completion",
  "  Ctrl+O           Toggle detail tool calls",
].join("\n");

export function commandResultFrame(name) {
  switch (name) {
    case "help":
      return {
        type: "command_result",
        name,
        ok: true,
        result: { kind: "text", body: HELP_BODY },
      };
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