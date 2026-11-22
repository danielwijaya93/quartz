/**
 * Transport abstraction for the Quartz web protocol.
 *
 * Everything above this file talks to a `Connection` and never to a WebSocket.
 * That is what keeps the app swappable: the mock server, a test double and the
 * real Python server all satisfy the same interface, and no component needs to
 * know which one is behind it.
 *
 * Responsibilities kept here (and nowhere else):
 *   - open/close the socket, with reconnect backoff
 *   - parse text frames into `ServerMessage` (unknown frames dropped, not thrown)
 *   - expose typed send helpers for every `ClientMessage`
 *   - announce connection status changes
 */

import {
  CLIENT_VERSION,
  parseServerMessage,
  type ClientMessage,
  type ImageAttachment,
  type ServerMessage,
} from "../protocol";

export type ConnectionStatus = "connecting" | "connected" | "disconnected";

export interface ConnectionEvents {
  message: ServerMessage;
  status: ConnectionStatus;
  /** Fired on every successful open, including reconnects, before `hello` is sent. */
  open: undefined;
}

type Listener<T> = (payload: T) => void;

/** Minimal `WebSocket` surface, so tests can inject a fake without a network. */
export interface SocketLike {
  readyState: number;
  send(data: string): void;
  close(code?: number, reason?: string): void;
  onopen: ((event: unknown) => void) | null;
  onclose: ((event: unknown) => void) | null;
  onerror: ((event: unknown) => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
}

export type SocketFactory = (url: string) => SocketLike;

export interface ConnectionOptions {
  /** Override socket construction. Defaults to `new WebSocket(url)`. */
  socketFactory?: SocketFactory;
  /** Override reconnect delays, for tests. Index is clamped to the last entry. */
  backoffMs?: readonly number[];
  /** Set false to disable reconnect (tests that assert a dead connection). */
  reconnect?: boolean;
}

const DEFAULT_BACKOFF_MS = [500, 1000, 2000, 4000, 8000] as const;

const OPEN = 1;

/**
 * A reconnecting client for the Quartz web protocol.
 *
 * `connect()` is idempotent-ish: calling it while open keeps the existing
 * connection and re-sends `hello`, which is what the reconcile path needs.
 */
export class Connection {
  private readonly url: string;
  private readonly socketFactory: SocketFactory;
  private readonly backoffMs: readonly number[];
  private readonly reconnectEnabled: boolean;

  private socket: SocketLike | null = null;
  private listeners: { [K in keyof ConnectionEvents]: Set<Listener<ConnectionEvents[K]>> } = {
    message: new Set(),
    status: new Set(),
    open: new Set(),
  };

  /**
   * System-prompt append for this session, carried on every `hello`.
   *
   * Stored on the connection rather than passed to `connect()` because the value
   * can change after the socket is open (the Rich UI toggle); `setSystemPromptAppend`
   * re-sends `hello`, which is the protocol's idempotent "here is my config" frame.
   */
  private systemPromptAppend = "";

  private attempt = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private currentStatus: ConnectionStatus = "disconnected";
  private closedByUser = false;

  constructor(url: string, options: ConnectionOptions = {}) {
    this.url = url;
    this.socketFactory =
      options.socketFactory ?? ((target: string) => new WebSocket(target) as unknown as SocketLike);
    this.backoffMs = options.backoffMs ?? DEFAULT_BACKOFF_MS;
    this.reconnectEnabled = options.reconnect ?? true;
  }

  get status(): ConnectionStatus {
    return this.currentStatus;
  }

  /* ----------------------------------------------------------- event api */

  on<K extends keyof ConnectionEvents>(
    event: K,
    listener: Listener<ConnectionEvents[K]>,
  ): () => void {
    this.listeners[event].add(listener);
    return () => {
      this.listeners[event].delete(listener);
    };
  }

  private emit<K extends keyof ConnectionEvents>(event: K, payload: ConnectionEvents[K]): void {
    for (const listener of this.listeners[event]) listener(payload);
  }

  private setStatus(next: ConnectionStatus): void {
    if (this.currentStatus === next) return;
    this.currentStatus = next;
    this.emit("status", next);
  }

  /* ------------------------------------------------------------- socket */

  connect(): void {
    this.closedByUser = false;
    if (this.socket && this.socket.readyState === OPEN) {
      this.sendHello();
      return;
    }
    this.clearTimer();
    this.setStatus("connecting");

    let socket: SocketLike;
    try {
      socket = this.socketFactory(this.url);
    } catch {
      this.setStatus("disconnected");
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;

    socket.onopen = () => {
      this.attempt = 0;
      this.setStatus("connected");
      this.emit("open", undefined);
      this.sendHello();
    };

    socket.onmessage = (event) => {
      if (typeof event.data !== "string") return;
      const message = parseServerMessage(event.data);
      if (!message) return;
      this.emit("message", message);
    };

    socket.onerror = () => {
      // `onclose` always follows, and that is where reconnect is scheduled.
    };

    socket.onclose = () => {
      this.socket = null;
      this.setStatus("disconnected");
      if (!this.closedByUser) this.scheduleReconnect();
    };
  }

  private scheduleReconnect(): void {
    if (!this.reconnectEnabled || this.closedByUser) return;
    if (this.timer !== null) return;
    const delay = this.backoffMs[Math.min(this.attempt, this.backoffMs.length - 1)] ?? 1000;
    this.attempt += 1;
    this.timer = setTimeout(() => {
      this.timer = null;
      this.connect();
    }, delay);
  }

  private clearTimer(): void {
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  close(): void {
    this.closedByUser = true;
    this.clearTimer();
    const socket = this.socket;
    this.socket = null;
    if (socket) {
      socket.onclose = null;
      socket.onmessage = null;
      socket.onerror = null;
      socket.onopen = null;
      socket.close(1000, "client closed");
    }
    this.setStatus("disconnected");
  }

  /* -------------------------------------------------------------- send */

  private send(message: ClientMessage): boolean {
    const socket = this.socket;
    if (!socket || socket.readyState !== OPEN) return false;
    socket.send(JSON.stringify(message));
    return true;
  }

  sendHello(): boolean {
    const append = this.systemPromptAppend;
    return this.send(
      append === ""
        ? { type: "hello", clientVersion: CLIENT_VERSION }
        : { type: "hello", clientVersion: CLIENT_VERSION, systemPromptAppend: append },
    );
  }

  /**
   * Set this session's system-prompt append and tell the server.
   *
   * A value change while connected re-sends `hello`; the server applies it to the
   * live runtime, so toggling Rich UI mid-session takes effect on the next turn
   * without rebuilding the session.
   */
  setSystemPromptAppend(append: string): boolean {
    this.systemPromptAppend = append;
    if (this.currentStatus !== "connected") return false;
    return this.sendHello();
  }

  sendPrompt(text: string, images: ImageAttachment[] = []): boolean {
    return this.send(
      images.length > 0 ? { type: "prompt", text, images } : { type: "prompt", text },
    );
  }

  sendSteer(text: string): boolean {
    return this.send({ type: "steer", text });
  }

  sendCancel(): boolean {
    return this.send({ type: "cancel" });
  }

  sendCommand(name: string, arg?: string): boolean {
    const bare = name.startsWith("/") ? name.slice(1) : name;
    return this.send(arg === undefined || arg === "" ? { type: "command", name: bare } : { type: "command", name: bare, arg });
  }

  /** Start a fresh session, in `cwd` when the sidebar picked a folder. */
  sendNewSession(cwd?: string): boolean {
    return this.send(cwd === undefined ? { type: "new_session" } : { type: "new_session", cwd });
  }

  sendTrustProject(): boolean {
    return this.send({ type: "trust_project" });
  }

  sendLoadSession(sessionId: string): boolean {
    return this.send({ type: "load_session", sessionId });
  }
}

/**
 * Resolve the WebSocket URL for the current origin.
 *
 * Same-origin by design: the SPA is served by `quartz web`, so no CORS and no
 * configurable endpoint. `VITE_WS_URL` exists only for the Vite dev server.
 */
export function resolveSocketUrl(location: {
  protocol: string;
  host: string;
}): string {
  const override = wsUrlOverride();
  if (typeof override === "string" && override !== "") return override;
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${location.host}/ws`;
}

/**
 * Vite inlines `import.meta.env.VITE_WS_URL` statically, so this must stay a
 * literal member expression — reading it through a variable silently disables the
 * replacement and the dev override stops working.
 */
function wsUrlOverride(): string | undefined {
  const value: unknown = import.meta.env.VITE_WS_URL;
  return typeof value === "string" && value !== "" ? value : undefined;
}