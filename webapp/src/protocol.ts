/**
 * Wire protocol for `quartz web` (v1, draft).
 *
 * This file is the single source of truth for message shapes on the client side.
 * It mirrors `docs/web-protocol.md` exactly; if the document changes, change this
 * file in the same commit. No React, no DOM: plain types plus tiny guards so the
 * reducer can be exercised headlessly.
 */

/** Wire protocol version the client speaks. Sent in `hello`. */
export const CLIENT_VERSION = 1;

/* ------------------------------------------------------------------ messages */

export type MessageContentPart =
  | { type: "text"; text: string }
  | { type: "image_url"; image_url?: { url?: string } }
  | { type: string; [key: string]: unknown };

/** Same shape the session log already stores. History is rendered from these. */
export interface Message {
  role: "user" | "assistant" | "system" | "tool";
  content: string | MessageContentPart[] | null;
  tool_calls?: ToolCallRecord[];
  tool_call_id?: string;
}

export interface ToolCallRecord {
  id: string;
  type?: string;
  function?: { name?: string; arguments?: string };
}

export type PermissionDecision = "allow_once" | "allow_session" | "reject";

export type WatcherMode = "auto" | "goal" | "loop" | "off";
export type WatcherStatus =
  | "inactive"
  | "idle"
  | "countdown"
  | "evaluating"
  | "paused"
  | "completed";
export type WatcherAction =
  | "continue_now"
  | "cancel_countdown"
  | "pause"
  | "resume"
  | "clear";

export interface WatcherState {
  configured: boolean;
  provider: string;
  model: string;
  mode: WatcherMode;
  status: WatcherStatus;
  goal: string;
  promptTemplate: string;
  delaySeconds: number;
  maxTurns: number;
  turnCount: number;
  nudgesUsed: number;
  maxNudges: number;
  paused: boolean;
  countdownRemaining: number;
  lastReason: string;
}

export interface PendingPermission {
  requestId: string;
  tool: string;
  command: string;
  cwd: string;
  scope: string;
}

export interface ImageAttachment {
  mimeType: string;
  /** Base64, no `data:` prefix. */
  data: string;
}

export interface FileAttachment {
  id: string;
  name: string;
  mimeType: string;
  size: number;
  url: string;
}

export interface Command {
  name: string;
  description: string;
  usage?: string;
  /**
   * The command exists, but only the terminal can run it: it opens a picker,
   * touches the clipboard or drives an OAuth flow. Optional because an older
   * server does not send it at all, and absent means "works here".
   */
  needsTerminal?: boolean;
}

/** `CommandResult.kind` mirrors the Python `Literal`. */
export type CommandResultKind = "text" | "list" | "error" | "needs_terminal";

export interface CommandListItem {
  label: string;
  detail?: string;
}

/**
 * Result payload produced by a transport-independent command handler.
 * `body` is markdown for `kind="text"`; `items` is only used by `kind="list"`;
 * `message` carries the explanation for `error` / `needs_terminal`.
 */
export interface CommandResult {
  kind: CommandResultKind;
  title?: string;
  body?: string;
  items?: CommandListItem[];
  message?: string;
}

export type TurnEndStatus = "ok" | "aborted" | "interrupted" | "waiting_permission" | "error";

/* ------------------------------------------------------- client -> server */

export type ClientMessage =
  /**
   * `systemPromptAppend` is the client's vendor extension, mirroring the ACP
   * `_meta.quartz.systemPrompt.append` field: text the server appends to the
   * Quartz system prompt for this session. The web client sends the OpenUI Lang
   * prompt here when Rich UI is on and omits it when Rich UI is off, so the model
   * is only ever told to emit openui-lang when the renderer is available.
   * Set once for the session; re-sent when the preference changes.
   *
   * `cwd` is where the initial session should run, for the folder-first sidebar.
   * It is honoured only while no session exists yet: `hello` is re-sent on every
   * preference change, and a re-sent path must not move a live session.
   */
  | {
      type: "hello";
      clientVersion: number;
      systemPromptAppend?: string;
      cwd?: string;
      sessionId?: string;
    }
  | { type: "prompt"; text: string; images?: ImageAttachment[] }
  | { type: "steer"; text: string }
  | { type: "cancel" }
  | { type: "command"; name: string; arg?: string }
  /** `cwd` is the folder picked in the sidebar; omit it for the server's own. */
  | { type: "new_session"; cwd?: string }
  | { type: "load_session"; sessionId: string }
  | { type: "trust_project" }
  | { type: "set_sandbox"; enabled: boolean }
  | { type: "permission_response"; requestId: string; decision: PermissionDecision }
  | { type: "idle_session_response"; sessionId: string; action: "keep" | "close" }
  | { type: "active_session_dismiss"; sessionId: string }
  | { type: "active_sessions_clear_read" }
  | { type: "notifications_read" }
  | {
      type: "watcher_configure";
      mode?: WatcherMode;
      goal?: string;
      promptTemplate?: string;
      delaySeconds?: number;
      maxTurns?: number;
    }
  | { type: "watcher_action"; action: WatcherAction };

export interface ProjectTrustState {
  cwd: string;
  trusted: boolean;
  files: string[];
  needsTrust: boolean;
}

/* ------------------------------------------------------- server -> client */

export interface SessionStateMessage {
  type: "session_state";
  sessionId: string;
  cwd: string;
  model: string;
  effort: string;
  contextPct?: number;
  maxContextTokens?: number;
  cacheHitPct?: number;
  messages: Message[];
  cellFiles?: Record<string, FileAttachment[]>;
  turnRunning: boolean;
  /**
   * The answer the running turn has written but not yet committed to `messages`.
   *
   * A client that connects mid-turn draws this, so the prose already on the
   * writer's screen is on the reader's too; the live deltas that follow append to
   * it. Empty when no turn is running, and absent from an older server.
   */
  streamingText?: string;
  projectTrust?: ProjectTrustState;
  commands: Command[];
  bgCount?: number;
  gitBranch?: string | null;
  sandboxed?: boolean;
  pendingPermission?: PendingPermission | null;
  watcher?: WatcherState | null;
  settings?: {
    notifications: boolean;
    notifyTurn?: boolean;
    notifyApproval?: boolean;
    notifyBackground?: boolean;
    notifyError?: boolean;
    richUi: boolean;
  };
}

export type ServerMessage =
  | SessionStateMessage
  | { type: "turn_start"; turnId: string }
  | { type: "status"; label: string | null }
  | { type: "text_delta"; text: string }
  | { type: "thinking_delta"; text: string }
  | { type: "tool_call"; id: string; code: string }
  | {
      type: "tool_result";
      id: string;
      ok: boolean;
      output: string;
      durationMs: number;
      images?: ImageAttachment[];
      files?: FileAttachment[];
      actions?: Record<string, unknown> | null;
    }
  | { type: "bash_notice"; text: string }
  | { type: "bg_count"; count: number }
  | { type: "steering"; text: string }
  | {
      type: "compaction";
      summary: string;
      messagesBefore: number;
      messagesAfter: number;
      tokensBefore: number;
      tokensAfter: number;
    }
  | {
      type: "turn_end";
      status: TurnEndStatus;
      tokens: number;
      elapsedMs: number;
      contextPct?: number;
      maxContextTokens?: number;
      cacheHitPct?: number;
      error?: string;
    }
  | {
      type: "permission_request";
      requestId: string;
      tool: string;
      command: string;
      cwd: string;
      scope: string;
    }
  | { type: "permission_resolved"; requestId: string; decision: PermissionDecision }
  | {
      type: "idle_session_prompt";
      sessionId: string;
      cwd: string;
      idleSeconds: number;
    }
  | { type: "idle_session_resolved"; sessionId: string; action: "keep" | "close" }
  | { type: "active_sessions_state"; items: ActiveSessionItem[] }
  | {
      type: "settings_state";
      notifications: boolean;
      notifyTurn?: boolean;
      notifyApproval?: boolean;
      notifyBackground?: boolean;
      notifyError?: boolean;
      richUi: boolean;
    }
  | { type: "notifications_cleared" }
  | { type: "watcher_state"; watcher: WatcherState }
  | { type: "error"; message: string; fatal: boolean }
  | { type: "command_result"; name: string; ok: boolean; result: CommandResult };

export type ActiveSessionKind = "working" | "waiting" | "permission" | "done" | "error";

export interface ActiveSessionItem {
  sessionId: string;
  cwd: string;
  title?: string;
  kind: ActiveSessionKind;
  read?: boolean;
  updatedAt: string;
  preview?: string;
  isBot?: boolean;
}

export type ServerMessageType = ServerMessage["type"];

/* ------------------------------------------------------------------ guards */

const SERVER_MESSAGE_TYPES: ReadonlySet<string> = new Set<ServerMessageType>([
  "session_state",
  "turn_start",
  "status",
  "text_delta",
  "thinking_delta",
  "tool_call",
  "tool_result",
  "bash_notice",
  "bg_count",
  "steering",
  "compaction",
  "turn_end",
  "permission_request",
  "permission_resolved",
  "idle_session_prompt",
  "idle_session_resolved",
  "active_sessions_state",
  "settings_state",
  "notifications_cleared",
  "watcher_state",
  "error",
  "command_result",
]);

/** Narrow an unknown parsed frame to a `ServerMessage`, keeping unknown types out. */
export function isServerMessage(value: unknown): value is ServerMessage {
  if (typeof value !== "object" || value === null) return false;
  const type = (value as { type?: unknown }).type;
  return typeof type === "string" && SERVER_MESSAGE_TYPES.has(type);
}

/** Parse a raw WebSocket text frame. Returns null for anything unrecognised. */
export function parseServerMessage(raw: string): ServerMessage | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  return isServerMessage(parsed) ? parsed : null;
}
