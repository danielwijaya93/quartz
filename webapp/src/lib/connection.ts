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

import { withAuthToken } from "./authToken";
import { configuredWsUrl } from "./serverUrl";
import {
  CLIENT_VERSION,
  parseServerMessage,
  type ClientMessage,
  type ImageAttachment,
  type ServerMessage,
  type WatcherAction,
  type WatcherMode,
} from "../protocol";

export type ConnectionStatus = "connecting" | "connected" | "disconnected";

export interface ConnectionEvents {
  message: ServerMessage;
  status: ConnectionStatus;
  /** Fired on every successful open, including reconnects, before `hello` is sent. */
  open: undefined;
  /** Fired when the server closes the socket with 4401 (Unauthorized). */
  authError: undefined;
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
  /** How long to wait for a server response on wake before treating an open socket as dead. */
  wakeCheckTimeoutMs?: number;
  /** Optional sessionId sent in the very first `hello` frame (used by multi-tile panes). */
  initialSessionId?: string;
}

const DEFAULT_BACKOFF_MS = [100, 300, 1000, 2000, 4000] as const;
const DEFAULT_WAKE_CHECK_TIMEOUT_MS = 2500;

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
  private readonly wakeCheckTimeoutMs: number;

  private socket: SocketLike | null = null;
  private listeners: { [K in keyof ConnectionEvents]: Set<Listener<ConnectionEvents[K]>> } = {
    message: new Set(),
    status: new Set(),
    open: new Set(),
    authError: new Set(),
  };

  /**
   * System-prompt append for this session, carried on every `hello`.
   *
   * Stored on the connection rather than passed to `connect()` because the value
   * can change after the socket is open (the Rich UI toggle); `setSystemPromptAppend`
   * re-sends `hello`, which is the protocol's idempotent "here is my config" frame.
   */
  private systemPromptAppend = "";
  private currentSessionId: string | undefined = undefined;

  private attempt = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private wakeWatchdog: ReturnType<typeof setTimeout> | null = null;
  private lifecycleAttached = false;
  private currentStatus: ConnectionStatus = "disconnected";
  private closedByUser = false;

  private readonly onVisibilityChange = (): void => {
    if (typeof document !== "undefined" && document.visibilityState === "visible") {
      this.reconnectNow();
    }
  };

  private readonly onOnline = (): void => {
    this.reconnectNow();
  };

  constructor(url: string, options: ConnectionOptions = {}) {
    this.url = url;
    this.socketFactory =
      options.socketFactory ?? ((target: string) => new WebSocket(target) as unknown as SocketLike);
    this.backoffMs = options.backoffMs ?? DEFAULT_BACKOFF_MS;
    this.reconnectEnabled = options.reconnect ?? true;
    this.wakeCheckTimeoutMs = options.wakeCheckTimeoutMs ?? DEFAULT_WAKE_CHECK_TIMEOUT_MS;
    if (options.initialSessionId) {
      this.currentSessionId = options.initialSessionId;
    }
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

  private attachLifecycle(): void {
    if (this.lifecycleAttached) return;
    this.lifecycleAttached = true;
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", this.onVisibilityChange);
    }
    if (typeof window !== "undefined") {
      window.addEventListener("online", this.onOnline);
    }
  }

  private detachLifecycle(): void {
    if (!this.lifecycleAttached) return;
    this.lifecycleAttached = false;
    if (typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", this.onVisibilityChange);
    }
    if (typeof window !== "undefined") {
      window.removeEventListener("online", this.onOnline);
    }
  }

  private clearWakeWatchdog(): void {
    if (this.wakeWatchdog !== null) {
      clearTimeout(this.wakeWatchdog);
      this.wakeWatchdog = null;
    }
  }

  private discardSocket(socket: SocketLike | null, code?: number, reason?: string): void {
    if (!socket) return;
    socket.onclose = null;
    socket.onmessage = null;
    socket.onerror = null;
    socket.onopen = null;
    try {
      socket.close(code, reason);
    } catch {
      // Ignore close errors on already-broken sockets.
    }
  }

  connect(): void {
    this.closedByUser = false;
    this.attachLifecycle();
    if (this.socket && this.socket.readyState === OPEN) {
      this.sendHello();
      return;
    }
    this.clearTimer();
    this.clearWakeWatchdog();
    this.setStatus("connecting");

    let socket: SocketLike;
    try {
      socket = this.socketFactory(withAuthToken(this.url));
    } catch {
      this.setStatus("disconnected");
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;

    socket.onopen = () => {
      this.attempt = 0;
      this.clearWakeWatchdog();
      this.setStatus("connected");
      this.emit("open", undefined);
      this.sendHello();
    };

    socket.onmessage = (event) => {
      this.clearWakeWatchdog();
      if (typeof event.data !== "string") return;
      const message = parseServerMessage(event.data);
      if (!message) return;
      if (message.type === "session_state" && message.sessionId) {
        this.currentSessionId = message.sessionId;
      }
      this.emit("message", message);
    };

    socket.onerror = () => {
      // `onclose` always follows, and that is where reconnect is scheduled.
    };

    socket.onclose = (event) => {
      this.clearWakeWatchdog();
      this.socket = null;
      this.setStatus("disconnected");
      const closeCode = (event as { code?: unknown } | undefined)?.code;
      if (closeCode === 4401) {
        this.emit("authError", undefined);
        return;
      }
      if (!this.closedByUser) this.scheduleReconnect();
    };
  }

  /**
   * Immediately verify or re-establish the connection (e.g. when a phone unlocks
   * or the network comes back online).
   */
  reconnectNow(): void {
    if (this.closedByUser || !this.reconnectEnabled) return;
    this.clearTimer();
    this.attempt = 0;

    if (this.socket && this.socket.readyState === OPEN) {
      if (!this.sendHello()) return;
      this.clearWakeWatchdog();
      this.wakeWatchdog = setTimeout(() => {
        this.wakeWatchdog = null;
        if (this.closedByUser) return;
        const stale = this.socket;
        this.socket = null;
        this.discardSocket(stale);
        this.attempt = 0;
        this.connect();
      }, this.wakeCheckTimeoutMs);
      return;
    }

    if (this.socket) {
      const stale = this.socket;
      this.socket = null;
      this.discardSocket(stale);
    }
    this.connect();
  }

  private scheduleReconnect(): void {
    if (!this.reconnectEnabled || this.closedByUser) return;
    if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
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
    this.clearWakeWatchdog();
    this.detachLifecycle();
    const socket = this.socket;
    this.socket = null;
    this.discardSocket(socket, 1000, "client closed");
    this.setStatus("disconnected");
  }

  /* -------------------------------------------------------------- send */

  private send(message: ClientMessage): boolean {
    const socket = this.socket;
    if (!socket || socket.readyState !== OPEN) return false;
    try {
      socket.send(JSON.stringify(message));
      return true;
    } catch {
      this.clearWakeWatchdog();
      this.socket = null;
      this.discardSocket(socket);
      this.setStatus("disconnected");
      if (!this.closedByUser) this.scheduleReconnect();
      return false;
    }
  }

  sendHello(): boolean {
    const append = this.systemPromptAppend;
    const frame: ClientMessage = {
      type: "hello",
      clientVersion: CLIENT_VERSION,
      ...(append === "" ? {} : { systemPromptAppend: append }),
      ...(this.currentSessionId ? { sessionId: this.currentSessionId } : {}),
    };
    return this.send(frame);
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
    this.currentSessionId = undefined;
    const append = this.systemPromptAppend;
    return this.send({
      type: "new_session",
      ...(cwd === undefined ? {} : { cwd }),
      ...(append === "" ? {} : { systemPromptAppend: append }),
    });
  }

  sendTrustProject(): boolean {
    return this.send({ type: "trust_project" });
  }

  sendSetSandbox(enabled: boolean): boolean {
    return this.send({ type: "set_sandbox", enabled });
  }

  sendSetRichUi(enabled: boolean, systemPromptAppend = ""): boolean {
    return this.send({
      type: "set_rich_ui",
      enabled,
      ...(enabled && systemPromptAppend ? { systemPromptAppend } : {}),
    });
  }

  sendPermissionResponse(
    requestId: string,
    decision: "allow_once" | "allow_session" | "reject",
  ): boolean {
    return this.send({ type: "permission_response", requestId, decision });
  }

  sendLoadSession(sessionId: string): boolean {
    this.currentSessionId = sessionId;
    const append = this.systemPromptAppend;
    return this.send({
      type: "load_session",
      sessionId,
      ...(append === "" ? {} : { systemPromptAppend: append }),
    });
  }

  sendActiveSessionDismiss(sessionId: string): boolean {
    return this.send({ type: "active_session_dismiss", sessionId });
  }

  sendActiveSessionsClearRead(): boolean {
    return this.send({ type: "active_sessions_clear_read" });
  }

  sendNotificationsRead(): boolean {
    return this.send({ type: "notifications_read" });
  }

  sendWatcherConfigure(config: {
    mode?: WatcherMode;
    goal?: string;
    promptTemplate?: string;
    delaySeconds?: number;
    maxTurns?: number;
  }): boolean {
    return this.send({ type: "watcher_configure", ...config });
  }

  sendWatcherAction(action: WatcherAction): boolean {
    return this.send({ type: "watcher_action", action });
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
  const configured = configuredWsUrl();
  if (configured) return configured;
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