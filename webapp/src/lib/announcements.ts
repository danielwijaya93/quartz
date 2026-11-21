/**
 * What the screen reader hears.
 *
 * The transcript streams roughly one text update per token, so putting a live
 * region on it means hundreds of announcements per turn - unusable with a screen
 * reader and pure noise without one. Instead the transcript is a static
 * `role="log"` structure, and *this* module reduces the row list to coarse,
 * meaningful events that are worth interrupting someone for: a turn ending, an
 * error, a compaction. What a turn is *currently* doing belongs to the tray
 * header, which changes once per step rather than once per token.
 */
import type { Row } from "./transcript";

const MAX_ANNOUNCEMENT_CHARS = 140;

function clip(text: string): string {
  const collapsed = text.replace(/\s+/g, " ").trim();
  return collapsed.length <= MAX_ANNOUNCEMENT_CHARS
    ? collapsed
    : `${collapsed.slice(0, MAX_ANNOUNCEMENT_CHARS - 1)}…`;
}

/**
 * The announcement a single terminal row deserves, if any.
 *
 * Tool calls are deliberately silent here. A live turn's tray header is already
 * a live region saying what the turn is doing, step by step; announcing every
 * finished call as well would be a second voice reading the same news, louder
 * and one call behind.
 */
function describeRow(row: Row): string | null {
  switch (row.kind) {
    case "turn_end":
      return row.status === "ok"
        ? `Turn finished in ${(row.elapsedMs / 1000).toFixed(1)} seconds.`
        : `Turn ${row.status}.`;
    case "error":
      return `Error: ${clip(row.message)}`;
    case "compaction":
      return `Context compacted to ${row.messagesAfter} messages.`;
    case "bash_notice":
      return `Background job update: ${clip(row.text)}`;
    default:
      return null;
  }
}

/**
 * The single most useful thing to say about one state transition.
 *
 * Deliberately returns at most one message: a pile of queued announcements is
 * not more informative than the newest one, and a screen reader cannot keep up
 * with the queue anyway.
 */
export function nextAnnouncement(previous: readonly Row[], next: readonly Row[]): string | null {
  const alreadySeen = new Set(previous.map((row) => row.id));

  // Walk backwards: the newest terminal row is the one a reader wants to hear.
  for (let index = next.length - 1; index >= 0; index -= 1) {
    const row = next[index];
    if (!row) continue;
    if (alreadySeen.has(row.id)) continue;
    const message = describeRow(row);
    if (message) return message;
  }
  return null;
}
