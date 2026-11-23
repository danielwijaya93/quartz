/**
 * Pure `@file` mention detection, ranking, and insertion for the composer.
 *
 * No React, no DOM: mirrors `commandText.ts` so the interaction is testable
 * without mounting the composer.
 */

export interface AtMentionMatch {
  /** Index of the `@` character in `text`. */
  start: number;
  /** Caret index where the token ends. */
  end: number;
  /** Text typed after `@` up to the caret. */
  query: string;
}

/**
 * Detect an active `@query` token ending at `caret`.
 *
 * `@` must be at the start of the text or immediately preceded by whitespace so
 * email addresses like `user@example.com` do not trigger the file picker.
 */
export function detectAtMention(text: string, caret: number): AtMentionMatch | null {
  const clamped = Math.max(0, Math.min(caret, text.length));
  const before = text.slice(0, clamped);
  const atIndex = before.lastIndexOf("@");
  if (atIndex === -1) return null;

  if (atIndex > 0 && !/\s/.test(before[atIndex - 1] ?? "")) {
    return null;
  }

  const query = before.slice(atIndex + 1);
  if (/\s/.test(query)) return null;

  return { start: atIndex, end: clamped, query };
}

/**
 * Score a relative path against `query`: lower is better, `null` means no match.
 *
 * Ranking order:
 *   0 — basename starts with query
 *   1 — path starts with query
 *   2 — basename contains query
 *   3 — path contains query
 *   4 — fuzzy subsequence match
 */
function scoreFile(path: string, needle: string): number | null {
  if (needle === "") return 0;
  const lowerPath = path.toLowerCase();
  const slash = lowerPath.lastIndexOf("/");
  const base = slash === -1 ? lowerPath : lowerPath.slice(slash + 1);

  if (base.startsWith(needle)) return 0;
  if (lowerPath.startsWith(needle)) return 1;
  if (base.includes(needle)) return 2;
  if (lowerPath.includes(needle)) return 3;

  let qi = 0;
  for (let i = 0; i < lowerPath.length && qi < needle.length; i += 1) {
    if (lowerPath[i] === needle[qi]) qi += 1;
  }
  return qi === needle.length ? 4 : null;
}

/** Filter and rank workspace file paths for a `@query`. */
export function filterMentionFiles(
  files: readonly string[],
  query: string,
  limit = 30,
): string[] {
  const needle = query.trim().toLowerCase();
  if (needle === "") return files.slice(0, limit);

  const scored: { path: string; score: number }[] = [];
  for (const path of files) {
    const score = scoreFile(path, needle);
    if (score !== null) scored.push({ path, score });
  }
  scored.sort((a, b) => {
    if (a.score !== b.score) return a.score - b.score;
    if (a.path.length !== b.path.length) return a.path.length - b.path.length;
    return a.path.localeCompare(b.path);
  });
  return scored.slice(0, limit).map((item) => item.path);
}

/** Replace the `@query` span with `@path ` and return the updated text + caret. */
export function insertAtMention(
  text: string,
  mention: AtMentionMatch,
  filePath: string,
): { nextText: string; nextCaret: number } {
  const insertion = `@${filePath} `;
  const nextText = `${text.slice(0, mention.start)}${insertion}${text.slice(mention.end)}`;
  return { nextText, nextCaret: mention.start + insertion.length };
}
