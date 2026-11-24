/**
 * Turn grouping, labels, diff stats, and stream-following helpers.
 */

import type { FileAttachment } from "../../protocol";
import type {
  AssistantRow,
  DiffStats,
  GroupOptions,
  Row,
  ThinkingRow,
  ToolRow,
  TranscriptItem,
  Turn,
  TurnStep,
} from "./types";

export function isTurn(item: TranscriptItem): item is Turn {
  return item.kind === "turn";
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
  const files: FileAttachment[] = [];
  const seenFileIds = new Set<string>();
  for (const row of rows) {
    if (row.kind !== "tool" || !row.files?.length) continue;
    for (const f of row.files) {
      if (seenFileIds.has(f.id)) continue;
      seenFileIds.add(f.id);
      files.push(f);
    }
  }
  return {
    kind: "turn",
    // Derived from the first row's id, never from a position: a disclosure keyed
    // by index would remount - and collapse - every time an earlier turn grew.
    id: `turn-${rows[0]!.id}`,
    steps: foldSteps(rows, answerIndex),
    answer: answerIndex === -1 ? null : (rows[answerIndex] as AssistantRow),
    files,
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
