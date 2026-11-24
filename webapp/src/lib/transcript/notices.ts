/**
 * Harness notice parsing.
 */

import type { HarnessNotice } from "./types";

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
