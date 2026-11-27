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
  FileAttachment,
  ImageAttachment,
  Message,
  ServerMessage,
} from "../../protocol";
import { parseHarnessNotice } from "./notices";
import {
  initialState,
  type AssistantRow,
  type Row,
  type RowKind,
  type ToolRow,
  type TranscriptState,
  type UserRow,
} from "./types";

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

function patchSession(
  state: TranscriptState,
  patch: Partial<TranscriptState["session"]>,
): TranscriptState {
  return { ...state, session: { ...state.session, ...patch } };
}

export function clearBgFailed(state: TranscriptState): TranscriptState {
  return (state.session.bgFailed ?? 0) === 0 ? state : patchSession(state, { bgFailed: 0 });
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

/**
 * Stamp `durationMs` onto an open thinking row when a non-thinking event
 * closes it, so settled work summaries know how long thinking took.
 */
function finalizeOpenThinking(rows: Row[]): Row[] {
  const last = rows[rows.length - 1];
  if (last?.kind === "thinking" && last.startedAt !== undefined && last.durationMs === undefined) {
    const updated = { ...last, durationMs: Math.max(0, Date.now() - last.startedAt) };
    return [...rows.slice(0, -1), updated];
  }
  return rows;
}

/** Append one row unless its id is already rendered. */
function appendRow(state: TranscriptState, row: Row): TranscriptState {
  if (state.seen.has(row.id)) return state;
  const baseRows = state.openRowId !== null ? finalizeOpenThinking(state.rows) : state.rows;
  const next = withRows(state, [...baseRows, row]);
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
  const baseRows = state.openRowId !== null ? finalizeOpenThinking(state.rows) : state.rows;
  const fresh = apply(make(), chunk);
  const next = withRows(state, [...baseRows, fresh]);
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
          files: [],
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
function buildHistoryRows(
  messages: Message[],
  cellFiles?: Record<string, FileAttachment[]>,
): Row[] {
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
            const attachedFiles = cellFiles?.[message.tool_call_id];
            rows[targetIdx] = {
              ...existing,
              output: text,
              images: images.length > 0 ? images : existing.images,
              files: attachedFiles && attachedFiles.length > 0 ? attachedFiles : existing.files,
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
function mergeHistory(
  state: TranscriptState,
  messages: Message[],
  cellFiles?: Record<string, FileAttachment[]>,
): TranscriptState {
  const incoming = buildHistoryRows(messages, cellFiles);
  const historyIndexById = new Map<string, number>();
  const freshUserIndices = new Map<string, number[]>();
  const freshAssistantIndices = new Map<string, number[]>();
  let freshCount = 0;

  for (let i = 0; i < incoming.length; i += 1) {
    const row = incoming[i]!;
    historyIndexById.set(row.id, i);
    if (state.seen.has(row.id)) continue;
    freshCount += 1;
    if (row.kind === "user") {
      const queue = freshUserIndices.get(row.text);
      if (queue) queue.push(i);
      else freshUserIndices.set(row.text, [i]);
    } else if (row.kind === "assistant") {
      const key = row.text.trim();
      const queue = freshAssistantIndices.get(key);
      if (queue) queue.push(i);
      else freshAssistantIndices.set(key, [i]);
    }
  }

  // A prompt is echoed locally the moment it is sent (the wire has no
  // `user_message` frame), and live assistant answers stream in before history
  // commits them. Match each local/live copy 1-to-1 to its incoming history
  // index so it is replaced *in place* -- preserving the relative order of
  // live-only rows like `thinking` and `turn_end` around it.
  type MergeEntry = { kind: "history"; index: number } | { kind: "live"; row: Row };
  const entries: MergeEntry[] = [];
  const placedIndices = new Set<number>();
  const existingById = new Map<string, Row>();
  const inheritedFormState = new Map<number, Record<string, unknown>>();
  let retiredCount = 0;

  for (const row of state.rows) {
    existingById.set(row.id, row);
    const directIndex = historyIndexById.get(row.id);
    if (directIndex !== undefined) {
      placedIndices.add(directIndex);
      entries.push({ kind: "history", index: directIndex });
      continue;
    }
    if (row.kind === "user" && isLocalRow(row.id)) {
      const matchedIndex = freshUserIndices.get(row.text)?.shift();
      if (matchedIndex !== undefined) {
        retiredCount += 1;
        placedIndices.add(matchedIndex);
        entries.push({ kind: "history", index: matchedIndex });
        continue;
      }
    } else if (row.kind === "assistant" && !row.id.startsWith("history-")) {
      const matchedIndex = freshAssistantIndices.get(row.text.trim())?.shift();
      if (matchedIndex !== undefined) {
        retiredCount += 1;
        placedIndices.add(matchedIndex);
        if (row.formState !== null) {
          inheritedFormState.set(matchedIndex, row.formState);
        }
        entries.push({ kind: "history", index: matchedIndex });
        continue;
      }
    }
    entries.push({ kind: "live", row });
  }

  if (freshCount === 0 && retiredCount === 0) return state;

  const reconciledHistory = incoming.map((row, index) => {
    const existing = existingById.get(row.id);
    if (existing !== undefined) return existing;
    if (row.kind === "assistant" && row.formState === null) {
      const formState = inheritedFormState.get(index);
      if (formState !== undefined) {
        return { ...row, formState };
      }
    }
    return row;
  });

  // Find where trailing live-only rows (e.g. an in-progress reply not yet in
  // history) begin: everything up through the last placed history row and any
  // `turn_end` that immediately closed it belongs before new history rows.
  let splitAt = 0;
  for (let i = 0; i < entries.length; i += 1) {
    if (entries[i]!.kind === "history") {
      splitAt = i + 1;
    }
  }
  while (splitAt < entries.length) {
    const entry = entries[splitAt]!;
    if (entry.kind === "live" && entry.row.kind === "turn_end") {
      splitAt += 1;
    } else {
      break;
    }
  }

  // Interleave any unplaced history rows (e.g. initial load or messages missed
  // while disconnected) right before the next placed history row, and place any
  // remaining unplaced history rows before trailing live rows.
  const merged: Row[] = [];
  let nextUnplaced = 0;
  for (let i = 0; i < splitAt; i += 1) {
    const entry = entries[i]!;
    if (entry.kind === "live") {
      merged.push(entry.row);
      continue;
    }
    while (nextUnplaced < entry.index) {
      if (!placedIndices.has(nextUnplaced)) {
        merged.push(reconciledHistory[nextUnplaced]!);
      }
      nextUnplaced += 1;
    }
    merged.push(reconciledHistory[entry.index]!);
  }
  while (nextUnplaced < reconciledHistory.length) {
    if (!placedIndices.has(nextUnplaced)) {
      merged.push(reconciledHistory[nextUnplaced]!);
    }
    nextUnplaced += 1;
  }
  for (let i = splitAt; i < entries.length; i += 1) {
    const entry = entries[i]!;
    if (entry.kind === "live") {
      merged.push(entry.row);
    }
  }

  return withRows(state, merged);
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
      const base = switched
        ? {
            ...initialState,
            session: state.session,
            ...(state.activeSessions !== undefined
              ? { activeSessions: state.activeSessions }
              : {}),
          }
        : state;
      const merged = withLiveText(mergeHistory(base, message.messages, message.cellFiles), message.streamingText);
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
          cacheHitPct: message.cacheHitPct ?? 0,
          commands: message.commands,
          projectTrust: message.projectTrust ?? null,
          bgCount: message.bgCount ?? 0,
          gitBranch: message.gitBranch ?? null,
          sandboxed: message.sandboxed ?? false,
          pendingPermission: message.pendingPermission ?? null,
          watcher: message.watcher ?? state.session.watcher ?? null,
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
        () => ({ kind: "thinking", id: makeId("thinking"), text: "", startedAt: Date.now() }),
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
        startedAt: Date.now(),
        durationMs: null,
        images: [],
        files: [],
        actions: null,
      });

    case "tool_result": {
      const update = {
        status: (message.ok ? "ok" : "failed") as "ok" | "failed",
        output: message.output,
        durationMs: message.durationMs,
        images: message.images ?? [],
        files: message.files ?? [],
        actions: message.actions ?? null,
      };
      if (!state.seen.has(message.id)) {
        return appendRow(state, { kind: "tool", id: message.id, code: "", ...update });
      }
      return patchToolRow(state, message.id, (row) => ({ ...row, ...update }));
    }

    case "watcher_state":
      return patchSession(state, { watcher: message.watcher });

    case "permission_request":
      return patchSession(state, {
        pendingPermission: {
          requestId: message.requestId,
          tool: message.tool,
          command: message.command,
          cwd: message.cwd,
          scope: message.scope,
        },
      });

    case "permission_resolved":
      return state.session.pendingPermission?.requestId === message.requestId
        ? patchSession(state, { pendingPermission: null })
        : state;

    case "bg_count":
      return patchSession(state, { bgCount: Math.max(0, message.count) });

    case "bash_notice": {
      const exitMatch = /\[bash-done\b[^\]]*\bexit:(-?\d+)/.exec(message.text);
      const nextState =
        exitMatch !== null && Number(exitMatch[1]) !== 0
          ? patchSession(state, { bgFailed: (state.session.bgFailed ?? 0) + 1 })
          : state;
      return appendRow(nextState, { kind: "bash_notice", id: makeId("bash"), text: message.text });
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
            message.contextPct !== undefined ||
            message.maxContextTokens !== undefined ||
            message.cacheHitPct !== undefined
              ? {
                  ...state.session,
                  ...(message.contextPct !== undefined ? { contextPct: message.contextPct } : {}),
                  ...(message.maxContextTokens !== undefined
                    ? { maxContextTokens: message.maxContextTokens }
                    : {}),
                  ...(message.cacheHitPct !== undefined ? { cacheHitPct: message.cacheHitPct } : {}),
                }
              : state.session,
        },
        {
          kind: "turn_end",
          id: makeId("turn-end"),
          status: message.status,
          tokens: message.tokens,
          ...(message.completionTokens === undefined
            ? {}
            : { completionTokens: message.completionTokens }),
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

    case "active_sessions_state":
      return { ...state, activeSessions: message.items };

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
