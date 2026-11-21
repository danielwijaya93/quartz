#!/usr/bin/env node
/**
 * Mock Quartz web server.
 *
 * Implements the *server* half of `docs/web-protocol.md` well enough to develop
 * and verify the SPA without an API key, a model, or the Python track. It is
 * deliberately dumb: a scripted replay on a timer, with frames shaped exactly as
 * the real server will shape them so the reducer is exercised for real.
 *
 *   npm run mock          # ws://127.0.0.1:8765
 *   npm run mock -- --port 9000 --fast
 *
 * It also accepts every client->server message and echoes a plausible reply, so
 * the composer, palette, steer path and new-session path can all be clicked
 * through by hand.
 */

import { WebSocketServer } from "ws";
import { COMMANDS, commandResultFrame, scriptedTurn, sessionState } from "./script.mjs";

const args = process.argv.slice(2);
const portArg = args.indexOf("--port");
const PORT = portArg !== -1 ? Number(args[portArg + 1]) : 8765;
const FAST = args.includes("--fast");
const DELAY = FAST ? 1 : Number(process.env.MOCK_DELAY ?? 12);

/* ------------------------------------------------------------------ server */

const server = new WebSocketServer({ port: PORT, host: "127.0.0.1" });
let sockets = 0;

server.on("connection", (socket) => {
  sockets += 1;
  log(`client connected (${sockets} open)`);

  let timers = [];
  const send = (frame) => {
    if (socket.readyState === socket.OPEN) socket.send(JSON.stringify(frame));
  };
  const clearTimers = () => {
    for (const timer of timers) clearTimeout(timer);
    timers = [];
  };

  socket.on("message", (raw) => {
    let message;
    try {
      message = JSON.parse(String(raw));
    } catch {
      return;
    }

    switch (message.type) {
      case "hello":
        send(sessionState());
        break;

      case "prompt": {
        // Echo the user turn so the transcript shows it, exactly as the real
        // server does before any deltas arrive.
        log(`prompt: ${String(message.text).slice(0, 60)}…`);
        clearTimers();
        const frames = scriptedTurn();
        frames.forEach((frame, index) => {
          const timer = setTimeout(() => send(frame), index * DELAY);
          timers.push(timer);
        });
        break;
      }

      case "steer":
        log(`steer: ${String(message.text).slice(0, 60)}`);
        send({ type: "steering", text: String(message.text) });
        break;

      case "cancel":
        log("cancel");
        clearTimers();
        send({ type: "status", label: null });
        send({ type: "turn_end", status: "aborted", tokens: 0, elapsedMs: 0 });
        break;

      case "command":
        log(`command: /${message.name} ${message.arg ?? ""}`.trim());
        send(commandResultFrame(String(message.name)));
        break;

      case "new_session":
        log("new_session");
        clearTimers();
        send({
          ...sessionState(),
          sessionId: `20261121-0930-mock-${Math.random().toString(16).slice(2, 10)}`,
          messages: [],
        });
        break;

      case "load_session":
        log(`load_session: ${message.sessionId}`);
        send(sessionState());
        break;

      default:
        log(`unhandled: ${message.type}`);
        break;
    }
  });

  socket.on("close", () => {
    sockets -= 1;
    clearTimers();
    log(`client disconnected (${sockets} open)`);
  });
});

function log(line) {
  process.stdout.write(`[mock] ${line}\n`);
}

server.on("error", (error) => {
  if (error.code === "EADDRINUSE") {
    log(`port ${PORT} is already in use — pass --port <n> or stop the other process`);
    process.exit(1);
  }
  throw error;
});

server.on("listening", () => {
  log(`listening on ws://127.0.0.1:${PORT}/ (Ctrl+C to stop)`);
  log(`replaying a ${scriptedTurn().length}-frame turn every prompt`);
});

process.on("SIGINT", () => {
  server.close(() => process.exit(0));
});
