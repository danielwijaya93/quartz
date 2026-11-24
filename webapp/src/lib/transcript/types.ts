/**
 * Transcript data models: rows, session metadata, reducer state, and turn grouping types.
 */

import type {
  ActiveSessionItem,
  Command,
  CommandResult,
  FileAttachment,
  ImageAttachment,
  PendingPermission,
  ProjectTrustState,
  TurnEndStatus,
} from "../../protocol";

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
  files?: FileAttachment[];
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
  cacheHitPct?: number;
  commands: Command[];
  projectTrust: ProjectTrustState | null;
  bgCount?: number;
  bgFailed?: number;
  gitBranch?: string | null;
  sandboxed?: boolean;
  pendingPermission?: PendingPermission | null;
}

export interface IdleSessionPrompt {
  sessionId: string;
  cwd: string;
  idleSeconds: number;
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
  idlePrompts?: IdleSessionPrompt[];
  activeSessions?: ActiveSessionItem[];
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
  /** Files attached by tools during this turn, lifted out for prominent download. */
  files?: FileAttachment[];
  /** The turn is running: the tray header shows the current step instead of a count. */
  live: boolean;
  /** Total elapsed turn duration in milliseconds when settled. */
  elapsedMs?: number | null;
}

/** A transcript renders as a mix of turns and rows that stand on their own. */
export type TranscriptItem = Turn | Row;

export interface GroupOptions {
  /** True between `turn_start` and `turn_end`; only the trailing turn can be live. */
  running?: boolean;
}

export interface DiffStats {
  additions: number;
  deletions: number;
}
