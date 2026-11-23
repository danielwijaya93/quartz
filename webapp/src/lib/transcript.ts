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
  Command,
  CommandResult,
  ImageAttachment,
  Message,
  ProjectTrustState,
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
  /**
   * OpenUI form state for this reply, when one has been rendered.
   *
   * Held on the row because the row *is* the transcript's source of truth: a
   * re-render, a theme flip or a rich-UI toggle must not reset a half-filled
   * form. Null for an ordinary text reply, so markdown mode never sees it.
   */
  formState: Record<string, unknown> | null;
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

/**
 * A notice the *application* wrote, drawn in the transcript's left column.
 *
 * The harness speaks in `user` messages because that is the only role the wire
 * lets it inject with: a resumed session opens with a `[python-state-restored]`
 * message, a finished background job arrives as `[bash-done pid:…]`, a scheduled
 * checkpoint as `[progress]`. Those are the harness talking, not the reader, and
 * rendering them right-aligned in a "You" bubble attributes the application's
 * words to the person using it.
 */
export interface SystemNoticeRow {
  kind: "system_notice";
  id: string;
  /** The bracketed tag without its brackets: `python-state-restored`. */
  tag: string;
  /** Everything after the tag line: the part a person reads. */
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

export type Row =
  | UserRow
  | AssistantRow
  | ThinkingRow
  | ToolRow
  | BashNoticeRow
  | SystemNoticeRow
  | SteeringRow
  | CompactionRow
  | ErrorRow
  | TurnEndRow
  | CommandRow;

export type RowKind = Row["kind"];

/** Connection-level facts the transcript needs but the socket owns. */
export interface SessionMeta {
  sessionId: string;
  cwd: string;
  model: string;
  effort: string;
  contextPct?: number;
  maxContextTokens?: number;
  commands: Command[];
  projectTrust: ProjectTrustState | null;
  bgCount?: number;
  bgFailed?: number;
  gitBranch?: string | null;
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
  contextPct: 0,
  maxContextTokens: 0,
  commands: [],
  projectTrust: null,
  gitBranch: null,
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
  if (!trimmed && images.length === 0) return state;
  const row: UserRow = { kind: "user", id: `${LOCAL_PREFIX}user-${makeId("u")}`, text: trimmed, images };
  return appendRow(state, row);
}

/**
 * Persist an OpenUI surface's form state onto its row.
 *
 * OpenUI reports state on every keystroke; without storing it, any re-render that
 * remounts the renderer (toggling Rich UI off and back on, for instance) would
 * silently reset a half-filled form. It lives on the row rather than being encoded
 * into the message text, because the text is what markdown mode renders -- form
 * state must never leak into it.
 */
export function clearBgFailed(state: TranscriptState): TranscriptState {
  if ((state.session.bgFailed ?? 0) === 0) return state;
  return {
    ...state,
    session: {
      ...state.session,
      bgFailed: 0,
    },
  };
}

export function setRowFormState(
  state: TranscriptState,
  id: string,
  formState: Record<string, unknown>,
): TranscriptState {
  const index = state.rows.findIndex((row) => row.id === id);
  const current = index === -1 ? undefined : state.rows[index];
  if (!current || current.kind !== "assistant") return state;
  const rows = state.rows.slice();
  rows[index] = { ...current, formState };
  return { ...state, rows, seen: state.seen };
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

/**
 * A harness notice read out of a user message.
 *
 * The tag is kept whole (`bash-done pid:4821 exit:0 duration:1.2s`) because its
 * attributes are the useful half of the line; `text` is the body underneath it.
 */
export interface HarnessNotice {
  /** The bracketed tag, brackets stripped. */
  tag: string;
  /** Everything after the tag's line, trimmed. */
  text: string;
}

/**
 * The tag line of a harness notice: `[python-state-restored]`,
 * `[bash-done pid:4821 exit:0]`.
 */
const HARNESS_NOTICE = /^\[([a-z][a-z0-9]*(?:-[a-z0-9]+){0,6}(?:\s[^\]\n]*)?)\]\s*\n?/;

/**
 * Read a harness notice out of a user message, or null when the user really spoke.
 *
 * `quartz` injects its own notices through the only role it has - `user` - and the
 * system prompt states the convention: "Messages starting with `[<kind>]` come from
 * the harness." (`core/prompt.py`.) The browser may use the same rule the model
 * does, which is why this is a pattern rather than a list of the three tags that
 * exist today: a fourth notice must arrive as a notice, not as a user bubble.
 *
 * Two characters keep a reader's own words out of it: the tag has to start at the
 * very first byte of the message and be at least three characters long, so "[x]
 * try this" is still something a person typed.
 */
export function parseHarnessNotice(text: string): HarnessNotice | null {
  const match = HARNESS_NOTICE.exec(text);
  const tag = match?.[1];
  if (match === null || tag === undefined) return null;
  const name = tag.split(/\s/)[0] ?? "";
  if (name.length < 3) return null;
  // A one-line notice ("[progress] Update the user now") carries its sentence
  // after the tag; a multiline one carries it below. A bare tag is a notice with
  // nothing to add, and the tag alone is the whole message.
  return { tag, text: text.slice(match[0].length).trim() };
}

/** Extract plain text and inline base64 images from string or multimodal content. */
function extractMessageContent(content: Message["content"]): {
  text: string;
  images: ImageAttachment[];
} {
  if (typeof content === "string") return { text: content, images: [] };
  if (!Array.isArray(content)) return { text: "", images: [] };
  const texts: string[] = [];
  const images: ImageAttachment[] = [];
  for (const part of content) {
    if (typeof part !== "object" || part === null) continue;
    if (part.type === "text" && typeof part.text === "string") {
      texts.push(part.text);
    } else if (part.type === "image_url") {
      const url = (part.image_url as { url?: unknown } | undefined)?.url;
      if (typeof url === "string") {
        const match = /^data:([^;]+);base64,(.+)$/s.exec(url);
        if (match?.[1] && match[2]) {
          images.push({ mimeType: match[1], data: match[2] });
        }
      }
    }
  }
  return { text: texts.join("\n"), images };
}

/** Unwrap JSON `{"code": "..."}` tool arguments, falling back to raw strings. */
function extractToolCode(rawArgs: string | undefined): string {
  if (!rawArgs) return "";
  try {
    const parsed = JSON.parse(rawArgs) as unknown;
    if (typeof parsed === "object" && parsed !== null && "code" in parsed) {
      const code = (parsed as { code?: unknown }).code;
      if (typeof code === "string") return code;
    }
  } catch {
    // Raw non-JSON arguments (e.g. "print(1)") fall back as-is.
  }
  return rawArgs;
}

/** Flatten a stored message into the rows it represents. */
function rowsFromMessage(message: Message, index: number): Row[] {
  const { text, images } = extractMessageContent(message.content);
  switch (message.role) {
    case "user": {
      // History carries harness notices as user messages; classifying them here is
      // what stops a resumed session from opening with the application quoted as
      // if the reader had said it.
      const notice = parseHarnessNotice(text);
      if (notice !== null) {
        return [
          {
            kind: "system_notice",
            id: `history-notice-${index}`,
            tag: notice.tag,
            text: notice.text,
          },
        ];
      }
      return [
        {
          kind: "user",
          id: `history-user-${index}`,
          text,
          images,
        },
      ];
    }
    case "assistant": {
      const rows: Row[] = [];
      if (text.trim()) {
        rows.push({
          kind: "assistant",
          id: `history-assistant-${index}`,
          text,
          formState: null,
        });
      }
      for (const [callIndex, call] of (message.tool_calls ?? []).entries()) {
        if (!call.id) continue;
        rows.push({
          kind: "tool",
          id: call.id,
          code: extractToolCode(call.function?.arguments),
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

/** Build history rows and attach `role: "tool"` outputs onto their matching ToolRows. */
function buildHistoryRows(messages: Message[]): Row[] {
  const rows: Row[] = [];
  const toolRowIndex = new Map<string, number>();
  for (let index = 0; index < messages.length; index += 1) {
    const message = messages[index]!;
    if (message.role === "tool") {
      if (message.tool_call_id) {
        const targetIdx = toolRowIndex.get(message.tool_call_id);
        if (targetIdx !== undefined) {
          const existing = rows[targetIdx];
          if (existing && existing.kind === "tool") {
            const { text, images } = extractMessageContent(message.content);
            rows[targetIdx] = {
              ...existing,
              output: text,
              images: images.length > 0 ? images : existing.images,
            };
          }
        }
      }
      continue;
    }
    for (const row of rowsFromMessage(message, index)) {
      if (row.kind === "tool") {
        toolRowIndex.set(row.id, rows.length);
      }
      rows.push(row);
    }
  }
  return rows;
}

/**
 * Load history into the transcript without disturbing rows already on screen.
 *
 * Dedupe is by row id, which is derived from message position in the *session
 * log*. Live rows keep their network ids, so a reconnect that replays history
 * adds only rows that were never rendered.
 */
function mergeHistory(state: TranscriptState, messages: Message[]): TranscriptState {
  const incoming = buildHistoryRows(messages);
  const fresh = incoming.filter((row) => !state.seen.has(row.id));

  // A prompt is echoed locally the moment it is sent (the wire has no
  // `user_message` frame), so history will eventually contain the same prompt
  // under a positional id. Retire the local copy by text, one for one, or a
  // reconnect shows every prompt twice. Similarly, retire live assistant rows
  // once history catches up with them (e.g. `session_state` on Rich UI toggle).
  const pendingUsers = new Map<string, number>();
  const pendingAssistants = new Map<string, number>();
  for (const row of fresh) {
    if (row.kind === "user") {
      pendingUsers.set(row.text, (pendingUsers.get(row.text) ?? 0) + 1);
    } else if (row.kind === "assistant") {
      const key = row.text.trim();
      pendingAssistants.set(key, (pendingAssistants.get(key) ?? 0) + 1);
    }
  }

  const retired = new Set<string>();
  const inheritedFormState = new Map<string, Record<string, unknown>>();
  for (const row of state.rows) {
    if (row.kind === "user" && isLocalRow(row.id)) {
      const left = pendingUsers.get(row.text) ?? 0;
      if (left > 0) {
        pendingUsers.set(row.text, left - 1);
        retired.add(row.id);
      }
    } else if (row.kind === "assistant" && !row.id.startsWith("history-")) {
      const key = row.text.trim();
      const left = pendingAssistants.get(key) ?? 0;
      if (left > 0) {
        pendingAssistants.set(key, left - 1);
        retired.add(row.id);
        if (row.formState !== null) {
          inheritedFormState.set(key, row.formState);
        }
      }
    }
  }

  if (fresh.length === 0 && retired.size === 0) return state;

  // History belongs above live rows: it is everything that happened before.
  const existingById = new Map(state.rows.map((row) => [row.id, row]));
  const historyIds = new Set(incoming.map((row) => row.id));
  const reconciledHistory = incoming.map((row) => {
    const existing = existingById.get(row.id);
    if (existing !== undefined) return existing;
    if (row.kind === "assistant" && row.formState === null) {
      const formState = inheritedFormState.get(row.text.trim());
      if (formState !== undefined) {
        inheritedFormState.delete(row.text.trim());
        return { ...row, formState };
      }
    }
    return row;
  });
  const liveRows = state.rows.filter((row) => !retired.has(row.id) && !historyIds.has(row.id));
  return withRows(state, [...reconciledHistory, ...liveRows]);
}

/**
 * Draw the text a running turn has written but not yet committed to history.
 *
 * A reconnect while an answer is being written replays `messages`, which stop at the
 * prompt: `core/loop.py` appends its assistant message only once a *step* completes.
 * Without this the page would reconnect onto a transcript that ends at the question,
 * with the answer continuing somewhere it cannot see -- the "chat no longer updates"
 * symptom, from the reader's side.
 *
 * Rows already on screen are kept, because the deltas that arrived on the previous
 * socket are as real as the ones in the snapshot. The live text is only *added*: it
 * goes after anything present, and is never allowed to duplicate an answer that
 * history already carries.
 */
function withLiveText(state: TranscriptState, streamingText: string | undefined): TranscriptState {
  const raw = streamingText ?? "";
  if (raw.trim() === "") return state;

  const last = state.rows[state.rows.length - 1];
  if (last !== undefined && last.kind === "assistant") {
    if (last.text.trim().endsWith(raw.trim())) {
      // The loop committed this text as a message a moment ago; history has it.
      return { ...state, openRowId: last.id };
    }
    if (raw.startsWith(last.text)) {
      const rows = state.rows.slice(0, -1);
      rows.push({ ...last, text: raw });
      return { ...state, rows, openRowId: last.id };
    }
  }
  const row: AssistantRow = {
    kind: "assistant",
    id: makeId("assistant"),
    text: raw,
    formState: null,
  };
  return { ...withRows(state, [...state.rows, row]), openRowId: row.id };
}

/* ------------------------------------------------------------------ reducer */

export function reduce(state: TranscriptState, message: ServerMessage): TranscriptState {
  switch (message.type) {
    case "session_state": {
      // History rows are identified by their position in *this* session's log, so a
      // different session's history reuses the same ids. Merging it into what is on
      // screen would drop every message it shares by position and interleave the
      // rest -- the sidebar switching sessions is what makes that reachable.
      const switched =
        state.session.sessionId !== "" && state.session.sessionId !== message.sessionId;
      const base = switched ? { ...initialState, session: state.session } : state;
      const merged = withLiveText(mergeHistory(base, message.messages), message.streamingText);
      return {
        ...merged,
        turnRunning: message.turnRunning,
        session: {
          sessionId: message.sessionId,
          cwd: message.cwd,
          model: message.model,
          effort: message.effort,
          contextPct: message.contextPct ?? 0,
          maxContextTokens: message.maxContextTokens ?? 0,
          commands: message.commands,
          projectTrust: message.projectTrust ?? null,
          bgCount: message.bgCount ?? 0,
          gitBranch: message.gitBranch ?? null,
        },
        status: message.turnRunning ? merged.status : null,
      };
    }

    case "turn_start": {
      // A mid-turn reconnect (`session_state` with `turnRunning: true` followed by
      // `turn_start`) attaches to the live row opened by `withLiveText` instead of
      // splitting the answer into two assistant rows.
      const reattaching = state.turnRunning && state.turnId === null;
      return {
        ...state,
        turnId: message.turnId,
        turnRunning: true,
        status: null,
        openRowId: reattaching ? state.openRowId : null,
        // A new turn starts clean; only a fatal error latches across turns.
      };
    }

    case "status":
      return { ...state, status: message.label };

    case "text_delta":
      return appendToTrailing(
        state,
        "assistant",
        () => ({ kind: "assistant", id: makeId("assistant"), text: "", formState: null }),
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

    case "bg_count":
      return {
        ...state,
        session: {
          ...state.session,
          bgCount: Math.max(0, message.count),
        },
      };

    case "bash_notice": {
      const exitMatch = /\[bash-done\b[^\]]*\bexit:(-?\d+)/.exec(message.text);
      const failed = exitMatch !== null && Number(exitMatch[1]) !== 0;
      const nextState = failed
        ? {
            ...state,
            session: {
              ...state.session,
              bgFailed: (state.session.bgFailed ?? 0) + 1,
            },
          }
        : state;
      return appendRow(nextState, {
        kind: "bash_notice",
        id: makeId("bash"),
        text: message.text,
      });
    }

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
          session:
            message.contextPct !== undefined || message.maxContextTokens !== undefined
              ? {
                  ...state.session,
                  ...(message.contextPct !== undefined ? { contextPct: message.contextPct } : {}),
                  ...(message.maxContextTokens !== undefined
                    ? { maxContextTokens: message.maxContextTokens }
                    : {}),
                }
              : state.session,
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

/* ------------------------------------------------------------ turn grouping */

/**
 * The tray's atom: a run of one kind of row, in the order it happened.
 *
 * A turn is folded into a handful of steps rather than one row per event. The
 * tray is a summary of what the turn *did*; a list that repeats every tool call
 * as its own line is just the old stacked cards with an extra click.
 */
export type TurnStep =
  /**
   * Consecutive thinking rows. The tray draws them as one line: the stream split
   * the thought at a boundary, the model did not split it into two thoughts.
   */
  | { kind: "thinking"; id: string; rows: ThinkingRow[] }
  /** Interim narration - the model talking between tool calls. */
  | { kind: "text"; id: string; row: AssistantRow }
  /** Consecutive tool calls. Folded, because "used 3 tools" is the fact worth reading. */
  | { kind: "tool_run"; id: string; rows: ToolRow[] };

/**
 * One turn of the conversation: its machinery (the steps) and its answer.
 *
 * The answer is lifted out of the steps on purpose. Everything else in a turn is
 * something the user did not ask to read; the answer is the only part that
 * belongs in the reading column.
 */
export interface Turn {
  kind: "turn";
  id: string;
  steps: TurnStep[];
  answer: AssistantRow | null;
  /** The turn is running: the tray header shows the current step instead of a count. */
  live: boolean;
  /** Total elapsed turn duration in milliseconds when settled. */
  elapsedMs?: number | null;
}

/** A transcript renders as a mix of turns and rows that stand on their own. */
export type TranscriptItem = Turn | Row;

export function isTurn(item: TranscriptItem): item is Turn {
  return item.kind === "turn";
}

export interface GroupOptions {
  /** True between `turn_start` and `turn_end`; only the trailing turn can be live. */
  running?: boolean;
}

/** Rows that belong to a turn, as opposed to markers *between* turns. */
function belongsToTurn(row: Row): row is AssistantRow | ThinkingRow | ToolRow {
  return row.kind === "assistant" || row.kind === "thinking" || row.kind === "tool";
}

/** Collapse to a single line and clip it, for a header that must stay one row tall. */
function clipOneLine(text: string, max: number): string {
  const collapsed = text.replace(/\s+/g, " ").trim();
  return collapsed.length <= max ? collapsed : `${collapsed.slice(0, Math.max(max - 1, 0))}…`;
}

/** A tool's first non-blank line: as close to a name as a kernel cell gets. */
export function firstLine(code: string): string {
  const line = code.split("\n").find((candidate) => candidate.trim() !== "");
  return line ? line.trim() : "";
}

/**
 * A tool call's nearest thing to a name.
 *
 * The wire carries code, not a tool name, so the header borrows the function the
 * cell calls: `open(...)` reads as "open", `df = pd.read_csv(...)` as "read_csv".
 * When a cell is not a call at all (`import pandas as pd`) the line itself is the
 * most honest label available.
 */
export function toolName(code: string): string {
  const line = firstLine(code);
  if (line === "") return "";
  const call = /^(?:[\w.]+\s*=\s*)?([A-Za-z_][\w.]*)\s*\(/.exec(line);
  const callee = call?.[1];
  if (callee !== undefined) {
    const parts = callee.split(".");
    return parts[parts.length - 1] ?? callee;
  }
  return clipOneLine(line, 40);
}

/** "Used 3 tools": the one fact a folded run needs, in or out of the header. */
export function toolRunLabel(rows: readonly ToolRow[]): string {
  return `Used ${rows.length} ${rows.length === 1 ? "tool" : "tools"}`;
}

/**
 * How one step reads as a single line: the tray header while the turn runs, and
 * a tool run's own trigger once the tray is open.
 *
 * A running tool leads with what it is doing, not with a count: on a live turn
 * the header *is* the progress indicator, so it has to name the current step.
 */
export function stepLabel(step: TurnStep): string {
  switch (step.kind) {
    case "thinking":
      return "Thinking";
    case "text":
      return clipOneLine(step.row.text, 80) || "Reply";
    case "tool_run": {
      const running = step.rows.find((row) => row.status === "running");
      if (running) {
        const name = toolName(running.code);
        return name === "" ? "Running a tool" : `Running ${name}`;
      }
      return toolRunLabel(step.rows);
    }
  }
}

/** The words a live tray header uses, lowercased: the vocabulary it owns. */
const TRAY_STATUS_WORDS: ReadonlySet<string> = new Set(["thinking", "running", "writing"]);

/**
 * Whether a server status label is news the turn tray is already telling.
 *
 * Two lines claim the same moment otherwise: the tray header says "Thinking"
 * (pulsing, with the transcript's one live region) while `StatusLine` prints the
 * server's own label, "Thinking... · running", with a second spinner. Both are
 * true, neither adds anything, and together they read as two turns doing two
 * things. The tray owns "what the turn is doing", so a label that opens with one
 * of its words is not drawn again - and anything else the server has to say
 * ("Compacting context…") still is.
 */
export function trayOwnsStatus(label: string | null, turnRunning: boolean): boolean {
  if (!turnRunning || label === null) return false;
  const first = label.trim().toLowerCase().split(/\s+/)[0] ?? "";
  return TRAY_STATUS_WORDS.has(first.replace(/[.…:]+$/, ""));
}

/**
 * What a settled turn did, in one phrase.
 *
 * The collapsed header used to count *steps* ("1 step") over a body that counted
 * *tools* ("Used 4 tools"): two units for one idea, and the smaller number was the
 * one a reader saw first. So the header names the work itself - "Thought",
 * "4 tools", or both - and never how many rows the tray happens to hold.
 *
 * Failed calls are counted separately, by `failedToolCount`: they are the one thing
 * that must be visible without opening anything. Interim narration is not counted
 * at all - it is the answer arriving in pieces, not work to audit.
 */
export function turnSummary(turn: Turn): string {
  let thought = false;
  let tools = 0;
  for (const step of turn.steps) {
    if (step.kind === "thinking") thought = true;
    else if (step.kind === "tool_run") tools += step.rows.length;
  }
  const parts: string[] = [];
  if (thought) parts.push("Thought");
  if (tools > 0) parts.push(`${tools} ${tools === 1 ? "tool" : "tools"}`);
  // Every row that can make a step also makes one of the two parts above, so this
  // is only reached by an empty turn - and a header may never be blank.
  return parts.length === 0 ? "Answered" : parts.join(", ");
}

/**
 * Failed tool calls anywhere in the turn.
 *
 * The collapsed header is the only place a failure can be seen without clicking,
 * so this count is the design's one non-negotiable: silent failure is worse than
 * any amount of noise.
 */
export function failedToolCount(turn: Turn): number {
  let failed = 0;
  for (const step of turn.steps) {
    if (step.kind !== "tool_run") continue;
    for (const row of step.rows) {
      if (row.status === "failed") failed += 1;
    }
  }
  return failed;
}

/** Fold a turn's rows into steps, leaving out the row promoted to the answer. */
function foldSteps(
  rows: readonly (AssistantRow | ThinkingRow | ToolRow)[],
  answerIndex: number,
): TurnStep[] {
  const steps: TurnStep[] = [];
  let index = 0;
  while (index < rows.length) {
    if (index === answerIndex) {
      index += 1;
      continue;
    }
    const row = rows[index]!;
    if (row.kind === "thinking") {
      const run: ThinkingRow[] = [];
      // A run is consecutive rows of one kind, so an intervening text or tool row
      // legitimately splits it in two.
      while (index < rows.length) {
        const candidate = rows[index]!;
        if (candidate.kind !== "thinking") break;
        run.push(candidate);
        index += 1;
      }
      steps.push({ kind: "thinking", id: `thinking-${run[0]!.id}`, rows: run });
      continue;
    }
    if (row.kind === "tool") {
      const run: ToolRow[] = [];
      while (index < rows.length) {
        const candidate = rows[index]!;
        if (candidate.kind !== "tool") break;
        run.push(candidate);
        index += 1;
      }
      steps.push({ kind: "tool_run", id: `tool-run-${run[0]!.id}`, rows: run });
      continue;
    }
    steps.push({ kind: "text", id: `text-${row.id}`, row });
    index += 1;
  }
  return steps;
}

/**
 * One turn's rows -> its steps plus its answer.
 *
 * The answer is the *last* assistant row with text. A model narrates, calls a
 * tool, narrates again, and only the final block is the reply; promoting the
 * first one would bury the answer above the work that produced it. An assistant
 * row with no text is a tool-only segment, so it stays in the steps.
 */
function buildTurn(rows: readonly (AssistantRow | ThinkingRow | ToolRow)[]): Turn {
  let answerIndex = -1;
  for (let index = rows.length - 1; index >= 0; index -= 1) {
    const row = rows[index]!;
    if (row.kind === "assistant" && row.text.trim() !== "") {
      answerIndex = index;
      break;
    }
  }
  return {
    kind: "turn",
    // Derived from the first row's id, never from a position: a disclosure keyed
    // by index would remount - and collapse - every time an earlier turn grew.
    id: `turn-${rows[0]!.id}`,
    steps: foldSteps(rows, answerIndex),
    answer: answerIndex === -1 ? null : (rows[answerIndex] as AssistantRow),
    live: false,
  };
}

/**
 * Fold the flat row list into turns.
 *
 * Only rows a turn can produce - assistant, thinking, tool - are folded. Every
 * other kind (`user`, `bash_notice`, `system_notice`, `steering`, `compaction`,
 * `error`, `command`, `turn_end`) is a marker *between* turns: it closes the open
 * turn and is returned in place, so those keep rendering exactly as before.
 */
export function groupTurns(rows: readonly Row[], options: GroupOptions = {}): TranscriptItem[] {
  const items: TranscriptItem[] = [];
  let open: (AssistantRow | ThinkingRow | ToolRow)[] = [];

  const closeTurn = (): void => {
    if (open.length === 0) return;
    items.push(buildTurn(open));
    open = [];
  };

  for (const row of rows) {
    if (belongsToTurn(row)) {
      open.push(row);
      continue;
    }
    closeTurn();
    if (row.kind === "turn_end" && row.elapsedMs > 0) {
      const prev = items[items.length - 1];
      if (prev !== undefined && isTurn(prev) && prev.elapsedMs == null) {
        items[items.length - 1] = { ...prev, elapsedMs: row.elapsedMs };
      }
    }
    items.push(row);
  }
  closeTurn();

  // Only the trailing turn can be live: anything after a turn closes it, which
  // is also why an error mid-turn settles the work that preceded it.
  const last = items[items.length - 1];
  if (options.running === true && last !== undefined && isTurn(last)) {
    items[items.length - 1] = { ...last, live: true };
  }
  return items;
}

export interface DiffStats {
  additions: number;
  deletions: number;
}

/** True when *text* looks like a unified diff (hunk header or file header pair). */
export function isUnifiedDiff(text: string): boolean {
  if (!text) return false;
  const lines = text.split("\n");
  let hasHunk = false;
  let hasMinusHeader = false;
  let hasPlusHeader = false;
  for (const line of lines) {
    if (/^@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@/.test(line)) {
      hasHunk = true;
      break;
    }
    if (line.startsWith("--- ")) hasMinusHeader = true;
    else if (line.startsWith("+++ ")) hasPlusHeader = true;
  }
  return hasHunk || (hasMinusHeader && hasPlusHeader);
}

/** Count added and removed lines inside a unified diff string. */
export function parseDiffStats(text: string): DiffStats | null {
  if (!isUnifiedDiff(text)) return null;
  let additions = 0;
  let deletions = 0;
  let inHunk = false;
  for (const line of text.split("\n")) {
    if (line.startsWith("@@ ")) {
      inHunk = true;
      continue;
    }
    if (line.startsWith("--- ") || line.startsWith("+++ ") || line.startsWith("diff --git ")) {
      inHunk = false;
      continue;
    }
    if (!inHunk) continue;
    if (line.startsWith("+")) additions += 1;
    else if (line.startsWith("-")) deletions += 1;
  }
  if (additions === 0 && deletions === 0) return null;
  return { additions, deletions };
}

/** Aggregate unified-diff additions/deletions across all tool outputs in a turn. */
export function turnDiffStats(turn: Turn): DiffStats | null {
  let additions = 0;
  let deletions = 0;
  for (const step of turn.steps) {
    if (step.kind !== "tool_run") continue;
    for (const row of step.rows) {
      const stats = parseDiffStats(row.output);
      if (stats) {
        additions += stats.additions;
        deletions += stats.deletions;
      }
    }
  }
  if (additions === 0 && deletions === 0) return null;
  return { additions, deletions };
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

/* ------------------------------------------------------- follow the stream */

/**
 * The newest row the *reader* spoke, or null when they have not spoken yet.
 *
 * Scrolling decisions read the row stream rather than a prop: a prompt sent from
 * the composer, from the command palette, or by an OpenUI form submit all land as
 * a `user` row, so "the reader just sent something" is a fact about the log, not
 * about which component happened to call the send.
 */
export function lastUserRowId(rows: readonly Row[]): string | null {
  for (let index = rows.length - 1; index >= 0; index -= 1) {
    const row = rows[index];
    if (row !== undefined && row.kind === "user") return row.id;
  }
  return null;
}

/** The part of a row that carries what it says, for identity-by-content. */
function spokenText(row: Row): string {
  switch (row.kind) {
    case "user":
    case "assistant":
    case "thinking":
    case "bash_notice":
    case "steering":
    case "system_notice":
      return row.text;
    case "tool":
      return `${row.code}\u0000${row.output}`;
    case "compaction":
      return row.summary;
    case "error":
      return row.message;
    case "turn_end":
      return `${row.status}\u0000${row.tokens}\u0000${row.elapsedMs}`;
    case "command":
      return `${row.name}\u0000${row.ok}`;
  }
}

/**
 * Whether two row lists are the same conversation.
 *
 * `session_state` for another session replaces the log wholesale. Row *ids* cannot
 * answer this on their own: history rows are identified by their position in a
 * session, so two sessions both open with `history-user-0`, and an id-only check
 * would call every switch "more of the same". Identity plus what the row says can:
 * growth keeps a row exactly as it was, a switch replaces its words.
 */
export function sameConversation(previous: readonly Row[], next: readonly Row[]): boolean {
  // Nothing to compare yet: a first render is not a switch.
  if (previous.length === 0) return true;
  // An emptied log is a new session, not a shrinking one.
  if (next.length === 0) return false;
  const before = new Map(previous.map((row) => [row.id, row]));
  return next.some((row) => {
    const was = before.get(row.id);
    return was !== undefined && was.kind === row.kind && spokenText(was) === spokenText(row);
  });
}
