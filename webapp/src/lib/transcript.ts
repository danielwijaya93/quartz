/**
 * Transcript reducer: folds wire events into a flat list of render rows.
 *
 * Pure and framework-free on purpose. The whole log is one vertical stream, so the
 * model is a *flat* array of rows, never nested conversations. Two rules keep it
 * honest:
 *
 *   1. Deltas append into the trailing open row of the same kind, so 200 text
 *      deltas produce one row, not 200.
 *   2. Message identity, not position, decides what is already rendered. Replaying
 *      `session_state` after a reconnect therefore cannot duplicate history.
 */

import type {
  A2uiEnvelope,
  Command,
  CommandResult,
  ImageAttachment,
  Message,
  ServerMessage,
  TurnEndStatus,
} from "../protocol";

/* --------------------------------------------------------------- row models */

export interface UserRow {
  kind: "user";
  /** Stable identity: dedupes a replayed history against live rows. */
  id: string;
  text: string;
  images: ImageAttachment[];
}

export interface AssistantRow {
  kind: "assistant";
  id: string;
  text: string;
}

export interface ThinkingRow {
  kind: "thinking";
  id: string;
  text: string;
}

export interface ToolRow {
  kind: "tool";
  id: string;
  code: string;
  status: "running" | "ok" | "failed";
  output: string;
  durationMs: number | null;
  images: ImageAttachment[];
  actions: Record<string, unknown> | null;
}

export interface BashNoticeRow {
  kind: "bash_notice";
  id: string;
  text: string;
}

export interface SteeringRow {
  kind: "steering";
  id: string;
  text: string;
}

export interface CompactionRow {
  kind: "compaction";
  id: string;
  summary: string;
  messagesBefore: number;
  messagesAfter: number;
  tokensBefore: number;
  tokensAfter: number;
}

export interface ErrorRow {
  kind: "error";
  id: string;
  message: string;
  fatal: boolean;
}

export interface TurnEndRow {
  kind: "turn_end";
  id: string;
  status: TurnEndStatus;
  tokens: number;
  elapsedMs: number;
  error?: string;
}

export interface CommandRow {
  kind: "command";
  id: string;
  name: string;
  ok: boolean;
  result: CommandResult;
}

/**
 * A generated UI surface, as it arrived.
 *
 * The row carries the envelopes verbatim and in order: rendering is the A2UI
 * renderer's job, and the reducer's only opinion is *where* the surface sits in
 * the transcript. `id` is derived from the frame (turn + surface), so a replayed
 * frame dedupes instead of stacking a second copy.
 */
export interface A2uiRow {
  kind: "a2ui";
  id: string;
  surfaceId: string;
  messages: A2uiEnvelope[];
}

export type Row =
  | UserRow
  | AssistantRow
  | ThinkingRow
  | ToolRow
  | BashNoticeRow
  | SteeringRow
  | CompactionRow
  | ErrorRow
  | TurnEndRow
  | CommandRow
  | A2uiRow;

export type RowKind = Row["kind"];

/** Connection-level facts the transcript needs but the socket owns. */
export interface SessionMeta {
  sessionId: string;
  cwd: string;
  model: string;
  effort: string;
  commands: Command[];
}

export interface TranscriptState {
  rows: Row[];
  /** Current turn, or null between turns. */
  turnId: string | null;
  /** Coarse status label from `status`; cleared by `status(null)`. */
  status: string | null;
  /** True from `turn_start` to `turn_end`. Drives steer-vs-prompt. */
  turnRunning: boolean;
  /** Latched when an `error` arrives with `fatal: true`; blocks the composer. */
  fatalError: string | null;
  /** Set of row ids already present, so dedupe is O(1) rather than a scan. */
  seen: ReadonlySet<string>;
  /**
   * The single row currently receiving deltas, or null.
   *
   * This is why a delta never leaks into history: after `turn_start` no row is
   * open, so the first delta always opens a fresh row even when the transcript
   * already ends in an assistant row from a previous turn.
   */
  openRowId: string | null;
  session: SessionMeta;
}

export const EMPTY_SESSION: SessionMeta = {
  sessionId: "",
  cwd: "",
  model: "",
  effort: "",
  commands: [],
};

const INITIAL_ROWS: Row[] = [];

export const initialState: TranscriptState = {
  rows: INITIAL_ROWS,
  turnId: null,
  status: null,
  turnRunning: false,
  fatalError: null,
  seen: new Set<string>(),
  openRowId: null,
  session: EMPTY_SESSION,
};

/* ------------------------------------------------------------------ helpers */

let idCounter = 0;

/** Deterministic-enough unique id for locally created rows. */
function makeId(prefix: string): string {
  idCounter += 1;
  return `${prefix}-${idCounter}`;
}

/** Locally created rows are prefixed, so history can retire them by text. */
const LOCAL_PREFIX = "local-";

function isLocalRow(id: string): boolean {
  return id.startsWith(LOCAL_PREFIX);
}

/**
 * Echo a prompt into the transcript as it is sent.
 *
 * The protocol has no `user_message` frame -- the server only stores the message
 * in the session -- so without this the user's own turn is invisible until a
 * reload. The id is local and the row is retired by `mergeHistory` when history
 * catches up, so the echo never duplicates.
 */
export function addUserPrompt(
  state: TranscriptState,
  text: string,
  images: ImageAttachment[] = [],
): TranscriptState {
  const trimmed = text.trim();
  if (!trimmed) return state;
  const row: UserRow = { kind: "user", id: `${LOCAL_PREFIX}user-${makeId("u")}`, text: trimmed, images };
  return appendRow(state, row);
}

/** Test seam: reset the id counter so assertions stay stable. */
export function resetIdCounter(): void {
  idCounter = 0;
}

/**
 * The reducer is written as a fold over rows, and `seen` mirrors the row ids.
 * Rebuilding `seen` costs one pass and keeps every transition a pure function of
 * (state, message) with no hidden mutable bookkeeping.
 */
function withRows(state: TranscriptState, rows: Row[]): TranscriptState {
  const next: TranscriptState = { ...state, rows, seen: new Set(rows.map((row) => row.id)) };
  // Closing a row must clear the pointer, or the next delta reopens it.
  if (next.openRowId !== null && !next.seen.has(next.openRowId)) next.openRowId = null;
  return next;
}

/** Append one row unless its id is already rendered. */
function appendRow(state: TranscriptState, row: Row): TranscriptState {
  if (state.seen.has(row.id)) return state;
  const next = withRows(state, [...state.rows, row]);
  return { ...next, openRowId: null };
}

/**
 * Append `chunk` to the open row when it is of `kind`, otherwise open a new one.
 * This is what collapses a few hundred deltas into a single row.
 *
 * Appending is gated on `openRowId`, not on "the last row happens to be the right
 * kind": a previous turn's assistant row must never absorb this turn's deltas.
 */
function appendToTrailing<K extends RowKind>(
  state: TranscriptState,
  kind: K,
  make: () => Extract<Row, { kind: K }>,
  apply: (row: Extract<Row, { kind: K }>, chunk: string) => Extract<Row, { kind: K }>,
  chunk: string,
): TranscriptState {
  const last = state.rows[state.rows.length - 1];
  if (last && last.id === state.openRowId && last.kind === kind) {
    const updated = apply(last as Extract<Row, { kind: K }>, chunk);
    const rows = state.rows.slice(0, -1);
    rows.push(updated);
    return { ...state, rows, seen: state.seen };
  }
  const fresh = apply(make(), chunk);
  const next = withRows(state, [...state.rows, fresh]);
  return { ...next, openRowId: fresh.id };
}

/** Tool rows are mutated in place by id, since `tool_result` closes a `tool_call`. */
function patchToolRow(
  state: TranscriptState,
  id: string,
  patch: (row: ToolRow) => ToolRow,
): TranscriptState {
  const index = state.rows.findIndex((row) => row.kind === "tool" && row.id === id);
  if (index === -1) return state;
  const rows = state.rows.slice();
  const current = rows[index];
  if (current === undefined || current.kind !== "tool") return state;
  rows[index] = patch(current);
  return { ...state, rows, seen: state.seen };
}

function commandName(name: string): string {
  return name.startsWith("/") ? name.slice(1) : name;
}

/** Flatten a stored message into the rows it represents. */
function rowsFromMessage(message: Message, index: number): Row[] {
  const text = typeof message.content === "string" ? message.content : "";
  switch (message.role) {
    case "user":
      return [
        {
          kind: "user",
          id: `history-user-${index}`,
          text,
          images: [],
        },
      ];
    case "assistant": {
      const rows: Row[] = [];
      if (text.trim()) {
        rows.push({ kind: "assistant", id: `history-assistant-${index}`, text });
      }
      for (const [callIndex, call] of (message.tool_calls ?? []).entries()) {
        if (!call.id) continue;
        rows.push({
          kind: "tool",
          id: call.id,
          code: call.function?.arguments ?? "",
          status: "ok",
          output: "",
          durationMs: null,
          images: [],
          actions: null,
          // Marked complete: history has no duration, and a spinner would lie.
          ...({} as Record<string, never>),
        });
        void callIndex;
      }
      return rows;
    }
    case "tool":
      return [];
    case "system":
      if (!text.trim()) return [];
      return [{ kind: "bash_notice", id: `history-system-${index}`, text }];
    default:
      return [];
  }
}

/**
 * Load history into the transcript without disturbing rows already on screen.
 *
 * Dedupe is by row id, which is derived from message position in the *session
 * log*. Live rows keep their network ids, so a reconnect that replays history
 * adds only rows that were never rendered.
 */
function mergeHistory(state: TranscriptState, messages: Message[]): TranscriptState {
  const incoming = messages.flatMap(rowsFromMessage);
  const fresh = incoming.filter((row) => !state.seen.has(row.id));

  // A prompt is echoed locally the moment it is sent (the wire has no
  // `user_message` frame), so history will eventually contain the same prompt
  // under a positional id. Retire the local copy by text, one for one, or a
  // reconnect shows every prompt twice.
  const pending = new Map<string, number>();
  for (const row of incoming) {
    if (row.kind === "user") pending.set(row.text, (pending.get(row.text) ?? 0) + 1);
  }
  const retired = new Set<string>();
  for (const row of state.rows) {
    if (row.kind !== "user" || !isLocalRow(row.id)) continue;
    const left = pending.get(row.text) ?? 0;
    if (left > 0) {
      pending.set(row.text, left - 1);
      retired.add(row.id);
    }
  }

  const before = state.rows.filter((row) => !retired.has(row.id));
  if (fresh.length === 0 && retired.size === 0) return state;

  // History belongs above live rows: it is everything that happened before.
  const historyIds = new Set(incoming.map((row) => row.id));
  const liveRows = before.filter((row) => !historyIds.has(row.id));
  const ordered = [...before.filter((row) => historyIds.has(row.id)), ...fresh, ...liveRows];
  return withRows(state, ordered);
}

/* ------------------------------------------------------------------ reducer */

export function reduce(state: TranscriptState, message: ServerMessage): TranscriptState {
  switch (message.type) {
    case "session_state": {
      const merged = mergeHistory(state, message.messages);
      return {
        ...merged,
        turnRunning: message.turnRunning,
        session: {
          sessionId: message.sessionId,
          cwd: message.cwd,
          model: message.model,
          effort: message.effort,
          commands: message.commands,
        },
        status: message.turnRunning ? merged.status : null,
      };
    }

    case "turn_start":
      return {
        ...state,
        turnId: message.turnId,
        turnRunning: true,
        status: null,
        openRowId: null,
        // A new turn starts clean; only a fatal error latches across turns.
      };

    case "status":
      return { ...state, status: message.label };

    case "text_delta":
      return appendToTrailing(
        state,
        "assistant",
        () => ({ kind: "assistant", id: makeId("assistant"), text: "" }),
        (row, chunk) => ({ ...row, text: row.text + chunk }),
        message.text,
      );

    case "thinking_delta":
      return appendToTrailing(
        state,
        "thinking",
        () => ({ kind: "thinking", id: makeId("thinking"), text: "" }),
        (row, chunk) => ({ ...row, text: row.text + chunk }),
        message.text,
      );

    case "tool_call":
      return appendRow(state, {
        kind: "tool",
        id: message.id,
        code: message.code,
        status: "running",
        output: "",
        durationMs: null,
        images: [],
        actions: null,
      });

    case "tool_result": {
      // A result without its call must not crash: render a completed row so the
      // output is not lost, which is what a resumed/misordered stream produces.
      if (!state.seen.has(message.id)) {
        return appendRow(state, {
          kind: "tool",
          id: message.id,
          code: "",
          status: message.ok ? "ok" : "failed",
          output: message.output,
          durationMs: message.durationMs,
          images: message.images ?? [],
          actions: message.actions ?? null,
        });
      }
      return patchToolRow(state, message.id, (row) => ({
        ...row,
        status: message.ok ? "ok" : "failed",
        output: message.output,
        durationMs: message.durationMs,
        images: message.images ?? [],
        actions: message.actions ?? null,
      }));
    }

    case "bash_notice":
      return appendRow(state, {
        kind: "bash_notice",
        id: makeId("bash"),
        text: message.text,
      });

    case "steering":
      return appendRow(state, {
        kind: "steering",
        id: makeId("steering"),
        text: message.text,
      });

    case "compaction":
      return appendRow(state, {
        kind: "compaction",
        id: makeId("compaction"),
        summary: message.summary,
        messagesBefore: message.messagesBefore,
        messagesAfter: message.messagesAfter,
        tokensBefore: message.tokensBefore,
        tokensAfter: message.tokensAfter,
      });

    case "turn_end":
      return appendRow(
        {
          ...state,
          turnId: null,
          turnRunning: false,
          status: null,
        },
        {
          kind: "turn_end",
          id: makeId("turn-end"),
          status: message.status,
          tokens: message.tokens,
          elapsedMs: message.elapsedMs,
          ...(message.error === undefined ? {} : { error: message.error }),
        },
      );

    case "error":
      return {
        ...appendRow(state, {
          kind: "error",
          id: makeId("error"),
          message: message.message,
          fatal: message.fatal,
        }),
        fatalError: message.fatal ? message.message : state.fatalError,
      };

    case "a2ui": {
      // Nothing here trusts the frame: a malformed payload still yields a row, so
      // the view can explain itself instead of the surface vanishing silently.
      const messages = Array.isArray(message.messages)
        ? message.messages.filter(
            (entry): entry is A2uiEnvelope =>
              typeof entry === "object" && entry !== null && !Array.isArray(entry),
          )
        : [];
      const surfaceId = typeof message.surfaceId === "string" ? message.surfaceId : "";
      // Turn + surface identifies the frame: the same frame twice is one surface.
      const id = `a2ui-${typeof message.turnId === "string" ? message.turnId : "unknown"}-${
        surfaceId || "unnamed"
      }`;
      return appendRow({ ...state, openRowId: null }, {
        kind: "a2ui",
        id,
        surfaceId,
        messages,
      });
    }

    case "command_result":
      return appendRow(state, {
        kind: "command",
        id: makeId("command"),
        name: commandName(message.name),
        ok: message.ok,
        result: message.result,
      });

    default: {
      // Exhaustive over the protocol union; an unknown future type is ignored
      // rather than thrown, matching the server's forward-compat stance.
      return state;
    }
  }
}

/** Fold a list of messages, for tests and for replaying a recorded session. */
export function reduceAll(
  state: TranscriptState,
  messages: readonly ServerMessage[],
): TranscriptState {
  return messages.reduce(reduce, state);
}

/** Count lines in a thinking blob, for the disclosure's summary. */
export function lineCount(text: string): number {
  if (!text) return 0;
  return text.split("\n").length;
}

/** Rough token estimate used only for display next to thinking. */
export function approxTokens(text: string): number {
  return Math.ceil(text.length / 4);
}
