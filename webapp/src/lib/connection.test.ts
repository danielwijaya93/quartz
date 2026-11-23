import { describe, expect, it, vi } from "vitest";
import { Connection, resolveSocketUrl, type SocketLike } from "./connection";
import type { ServerMessage } from "../protocol";

/** A controllable stand-in for a WebSocket, so no test touches a network. */
class FakeSocket implements SocketLike {
  readyState = 0;
  sent: string[] = [];
  onopen: ((event: unknown) => void) | null = null;
  onclose: ((event: unknown) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    this.readyState = 3;
    this.onclose?.({});
  }

  open(): void {
    this.readyState = 1;
    this.onopen?.({});
  }

  emit(frame: ServerMessage | string): void {
    this.onmessage?.({ data: typeof frame === "string" ? frame : JSON.stringify(frame) });
  }
}

function makeConnection(): { connection: Connection; sockets: FakeSocket[] } {
  const sockets: FakeSocket[] = [];
  const connection = new Connection("ws://test/ws", {
    socketFactory: () => {
      const socket = new FakeSocket();
      sockets.push(socket);
      return socket;
    },
    backoffMs: [1],
    reconnect: false,
  });
  return { connection, sockets };
}

describe("Connection", () => {
  it("sends hello on open with the client version", () => {
    const { connection, sockets } = makeConnection();
    connection.connect();
    sockets[0]?.open();
    expect(JSON.parse(sockets[0]?.sent[0] ?? "{}")).toEqual({ type: "hello", clientVersion: 1 });
  });

  it("carries the system-prompt append on hello, and only when it is set", () => {
    const { connection, sockets } = makeConnection();
    connection.setSystemPromptAppend("ROOT IS STACK");
    connection.connect();
    sockets[0]?.open();

    expect(JSON.parse(sockets[0]?.sent[0] ?? "{}")).toEqual({
      type: "hello",
      clientVersion: 1,
      systemPromptAppend: "ROOT IS STACK",
    });
  });

  it("re-sends hello when the append changes, so the toggle reaches the server", () => {
    const { connection, sockets } = makeConnection();
    connection.connect();
    sockets[0]?.open();
    const before = sockets[0]?.sent.length ?? 0;

    connection.setSystemPromptAppend("APPEND");
    const after = sockets[0]?.sent ?? [];
    expect(after.length).toBe(before + 1);
    expect(JSON.parse(after[after.length - 1] ?? "{}")).toMatchObject({
      type: "hello",
      systemPromptAppend: "APPEND",
    });

    // Withdrawing it sends a hello with no append at all: "no append" must mean
    // absent, not an empty field, so the server's stock prompt applies.
    connection.setSystemPromptAppend("");
    expect(JSON.parse(after[after.length - 1] ?? "{}")).toEqual({ type: "hello", clientVersion: 1 });
  });

  it("remembers the append for the next socket, without sending into a closed one", () => {
    const { connection, sockets } = makeConnection();
    connection.setSystemPromptAppend("APPEND");
    expect(sockets).toHaveLength(0);

    connection.connect();
    sockets[0]?.open();
    expect(JSON.parse(sockets[0]?.sent[0] ?? "{}")).toMatchObject({
      systemPromptAppend: "APPEND",
    });
  });

  it("reports status transitions", () => {
    const { connection, sockets } = makeConnection();
    const seen: string[] = [];
    connection.on("status", (status) => seen.push(status));
    connection.connect();
    sockets[0]?.open();
    connection.close();
    expect(seen).toEqual(["connecting", "connected", "disconnected"]);
  });

  it("emits parsed server messages and drops junk", () => {
    const { connection, sockets } = makeConnection();
    const received: ServerMessage[] = [];
    connection.on("message", (message) => received.push(message));
    connection.connect();
    sockets[0]?.open();
    sockets[0]?.emit({ type: "text_delta", text: "hi" });
    sockets[0]?.emit("not json");
    sockets[0]?.emit(JSON.stringify({ type: "unknown_future" }));
    sockets[0]?.emit(JSON.stringify({ nope: true }));
    expect(received).toEqual([{ type: "text_delta", text: "hi" }]);
  });

  it("serialises each send helper to the documented shape", () => {
    const { connection, sockets } = makeConnection();
    connection.connect();
    sockets[0]?.open();
    sockets[0]!.sent.length = 0;

    connection.sendPrompt("hello");
    connection.sendPrompt("with image", [{ mimeType: "image/png", data: "AAA" }]);
    connection.sendSteer("go left");
    connection.sendCancel();
    connection.sendCommand("/settings", "progress 5");
    connection.sendCommand("jobs");
    connection.sendNewSession();
    connection.sendLoadSession("s-9");

    expect(sockets[0]?.sent.map((frame) => JSON.parse(frame))).toEqual([
      { type: "prompt", text: "hello" },
      { type: "prompt", text: "with image", images: [{ mimeType: "image/png", data: "AAA" }] },
      { type: "steer", text: "go left" },
      { type: "cancel" },
      { type: "command", name: "settings", arg: "progress 5" },
      { type: "command", name: "jobs" },
      { type: "new_session" },
      { type: "load_session", sessionId: "s-9" },
    ]);
  });

  it("returns false instead of throwing when the socket is not open", () => {
    const { connection } = makeConnection();
    expect(connection.sendPrompt("x")).toBe(false);
  });

  it("reconnects with backoff and re-sends hello", async () => {
    const sockets: FakeSocket[] = [];
    const connection = new Connection("ws://test/ws", {
      socketFactory: () => {
        const socket = new FakeSocket();
        sockets.push(socket);
        return socket;
      },
      backoffMs: [1],
      reconnect: true,
    });
    connection.connect();
    sockets[0]?.open();
    expect(sockets).toHaveLength(1);

    sockets[0]?.close();
    await vi.waitFor(() => expect(sockets).toHaveLength(2));
    sockets[1]?.open();
    expect(JSON.parse(sockets[1]?.sent[0] ?? "{}")).toEqual({ type: "hello", clientVersion: 1 });
    connection.close();
  });

  it("re-sends hello when connect() is called on an open socket", () => {
    const { connection, sockets } = makeConnection();
    connection.connect();
    sockets[0]?.open();
    sockets[0]!.sent.length = 0;
    connection.connect();
    expect(JSON.parse(sockets[0]?.sent[0] ?? "{}")).toMatchObject({ type: "hello" });
  });

  it("unsubscribes listeners", () => {
    const { connection, sockets } = makeConnection();
    const listener = vi.fn();
    const off = connection.on("message", listener);
    connection.connect();
    sockets[0]?.open();
    off();
    sockets[0]?.emit({ type: "text_delta", text: "ignored" });
    expect(listener).not.toHaveBeenCalled();
  });

  it("reconnects immediately on visibilitychange to visible without waiting on long backoff", () => {
    vi.useFakeTimers();
    try {
      const sockets: FakeSocket[] = [];
      const connection = new Connection("ws://test/ws", {
        socketFactory: () => {
          const socket = new FakeSocket();
          sockets.push(socket);
          return socket;
        },
        backoffMs: [8000],
        reconnect: true,
      });
      connection.connect();
      sockets[0]?.open();
      sockets[0]?.close();
      expect(sockets).toHaveLength(1);

      // Even though backoff is 8000ms, unlocking the screen reconnects right away.
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        value: "visible",
      });
      document.dispatchEvent(new Event("visibilitychange"));
      expect(sockets).toHaveLength(2);
      connection.close();
    } finally {
      vi.useRealTimers();
    }
  });

  it("pauses background reconnect attempts while hidden, then reconnects on visible", () => {
    vi.useFakeTimers();
    try {
      const sockets: FakeSocket[] = [];
      const connection = new Connection("ws://test/ws", {
        socketFactory: () => {
          const socket = new FakeSocket();
          sockets.push(socket);
          return socket;
        },
        backoffMs: [100, 500],
        reconnect: true,
      });
      connection.connect();
      sockets[0]?.open();

      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        value: "hidden",
      });
      sockets[0]?.close();
      vi.advanceTimersByTime(5000);
      expect(sockets).toHaveLength(1);

      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        value: "visible",
      });
      document.dispatchEvent(new Event("visibilitychange"));
      expect(sockets).toHaveLength(2);
      connection.close();
    } finally {
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        value: "visible",
      });
      vi.useRealTimers();
    }
  });

  it("re-sends hello on wake and replaces zombie sockets that do not respond", () => {
    vi.useFakeTimers();
    try {
      const sockets: FakeSocket[] = [];
      const connection = new Connection("ws://test/ws", {
        socketFactory: () => {
          const socket = new FakeSocket();
          sockets.push(socket);
          return socket;
        },
        backoffMs: [100],
        wakeCheckTimeoutMs: 500,
        reconnect: true,
      });
      connection.connect();
      sockets[0]?.open();
      sockets[0]!.sent.length = 0;

      // Wake when socket still thinks it is OPEN: sends hello to probe.
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        value: "visible",
      });
      document.dispatchEvent(new Event("visibilitychange"));
      expect(sockets[0]?.sent).toHaveLength(1);
      expect(JSON.parse(sockets[0]?.sent[0] ?? "{}")).toMatchObject({ type: "hello" });

      // If server responds before watchdog, keep the same socket.
      sockets[0]?.emit({ type: "text_delta", text: "alive" });
      vi.advanceTimersByTime(600);
      expect(sockets).toHaveLength(1);

      // Next wake where server does NOT respond -> zombie socket replaced.
      document.dispatchEvent(new Event("visibilitychange"));
      vi.advanceTimersByTime(500);
      expect(sockets).toHaveLength(2);
      connection.close();
    } finally {
      vi.useRealTimers();
    }
  });

  it("reconnects immediately when the browser fires online", () => {
    vi.useFakeTimers();
    try {
      const sockets: FakeSocket[] = [];
      const connection = new Connection("ws://test/ws", {
        socketFactory: () => {
          const socket = new FakeSocket();
          sockets.push(socket);
          return socket;
        },
        backoffMs: [8000],
        reconnect: true,
      });
      connection.connect();
      sockets[0]?.open();
      sockets[0]?.close();
      expect(sockets).toHaveLength(1);

      window.dispatchEvent(new Event("online"));
      expect(sockets).toHaveLength(2);
      connection.close();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("resolveSocketUrl", () => {
  it("uses ws for http and wss for https", () => {
    expect(resolveSocketUrl({ protocol: "http:", host: "127.0.0.1:8765" })).toBe(
      "ws://127.0.0.1:8765/ws",
    );
    expect(resolveSocketUrl({ protocol: "https:", host: "example.test" })).toBe(
      "wss://example.test/ws",
    );
  });
});