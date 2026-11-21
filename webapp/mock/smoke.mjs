
/**
 * End-to-end smoke test: boot the mock server, drive it as the SPA would, and
 * assert the frame sequence. Run with `node mock/smoke.mjs`.
 *
 * This is the check that the UI can be verified headlessly with no API key.
 */
import { spawn } from "node:child_process";
import { createServer } from "node:net";
import { WebSocket } from "ws";

/** Bind an ephemeral port so the smoke test never collides with a dev mock. */
const probe = createServer();
const PORT = await new Promise((resolve, reject) => {
  probe.once("error", reject);
  probe.listen(0, "127.0.0.1", () => {
    const address = probe.address();
    const port = typeof address === "object" && address !== null ? address.port : 0;
    probe.close(() => resolve(port));
  });
});

const child = spawn(process.execPath, ["mock/server.mjs", "--port", String(PORT), "--fast"], {
  stdio: ["ignore", "pipe", "inherit"],
});

const fail = (message) => {
  console.error(`SMOKE FAIL: ${message}`);
  child.kill();
  process.exit(1);
};

const ready = new Promise((resolve, reject) => {
  child.stdout.on("data", (chunk) => {
    if (String(chunk).includes("listening")) resolve();
  });
  child.once("exit", (code) => reject(new Error(`mock exited early with ${code}`)));
});

await ready;

const frames = [];
const socket = new WebSocket(`ws://127.0.0.1:${PORT}/`);

const done = new Promise((resolve) => {
  socket.on("message", (raw) => {
    const frame = JSON.parse(String(raw));
    frames.push(frame);
    if (frame.type === "turn_end") resolve();
  });
});

socket.on("open", () => {
  socket.send(JSON.stringify({ type: "hello", clientVersion: 1 }));
  setTimeout(() => socket.send(JSON.stringify({ type: "prompt", text: "smoke" })), 20);
});

await Promise.race([
  done,
  new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), 15000)),
]).catch((error) => fail(String(error)));

/* Assertions mirror `docs/web-protocol.md`. */
const byType = (type) => frames.filter((frame) => frame.type === type);

const state = byType("session_state")[0];
if (!state) fail("no session_state");
if (!state.sessionId || !state.cwd) fail("session_state missing sessionId/cwd");
if (!Array.isArray(state.messages)) fail("session_state missing messages");
if (!Array.isArray(state.commands) || state.commands.length === 0) fail("no commands advertised");

if (byType("turn_start").length !== 1) fail("expected exactly one turn_start");
if (byType("turn_end").length !== 1) fail("expected exactly one turn_end");
if (byType("text_delta").length < 10) fail("expected a stream of text deltas");
if (byType("thinking_delta").length === 0) fail("expected thinking deltas");
if (byType("bash_notice").length === 0) fail("expected a bash_notice");
if (byType("steering").length === 0) fail("expected a steering row");
if (byType("compaction").length === 0) fail("expected a compaction");

const calls = byType("tool_call");
const results = byType("tool_result");
if (calls.length < 3) fail("expected at least three tool calls");
if (results.length !== calls.length) fail("every tool_call must have a tool_result");
for (const result of results) {
  const call = calls.find((candidate) => candidate.id === result.id);
  if (!call) fail(`tool_result ${result.id} has no matching tool_call`);
  if (frames.indexOf(call) > frames.indexOf(result)) fail(`tool_result ${result.id} precedes its call`);
}
if (!results.some((result) => result.ok === false)) fail("expected one failing tool call");
if (!results.some((result) => (result.images ?? []).length > 0)) fail("expected an image result");

const turnEnd = byType("turn_end")[0];
if (turnEnd.status !== "ok") fail("turn_end status should be ok");
if (typeof turnEnd.tokens !== "number" || typeof turnEnd.elapsedMs !== "number") {
  fail("turn_end must carry tokens and elapsedMs");
}

/* Ordering: turn_start first, turn_end last. */
if (frames.findIndex((f) => f.type === "turn_start") > frames.findIndex((f) => f.type === "text_delta")) {
  fail("turn_start must precede the first delta");
}
if (frames.at(-1).type !== "turn_end") fail("turn_end must be the last frame of the turn");

/* The command path, all four kinds. */
for (const [name, kind] of [
  ["usage", "text"],
  ["jobs", "list"],
  ["model", "needs_terminal"],
  ["nope", "error"],
]) {
  const reply = await new Promise((resolve) => {
    socket.once("message", (raw) => resolve(JSON.parse(String(raw))));
    socket.send(JSON.stringify({ type: "command", name }));
  });
  if (reply.type !== "command_result") fail(`/${name} did not return command_result`);
  if (reply.result.kind !== kind) fail(`/${name} returned ${reply.result.kind}, expected ${kind}`);
}

console.log(`SMOKE OK — ${frames.length} frames, ${calls.length} tool calls, all four command kinds`);
socket.close();
child.kill();
process.exit(0);
