/**
 * OpenUI Lang detection and clean-up.
 *
 * The prompt forbids markdown fences around openui-lang, and models add them
 * anyway often enough that treating it as an error is not an option: a fenced
 * reply would render as a code block of DSL instead of a surface. So the client
 * strips a single outer fence before parsing, and asks this module -- never a
 * regex inline in a component -- what the text actually is.
 *
 * Ported from the proven Antigravity implementation; the ACP-specific builders
 * (`questionsToOpenUILang` and friends) are deliberately not carried over,
 * because quartz's web protocol has no `ask_question` tool payload to convert.
 */

/** Escapes a string literal for openui-lang double-quoted strings. */
export function quoteOpenUI(value: string): string {
  return JSON.stringify(value);
}

/**
 * Strip accidental markdown code fences around OpenUI Lang output.
 *
 * While streaming, the closing fence has usually not arrived yet, so it is only
 * removed once it is actually present -- otherwise every delta would re-trim the
 * same text and a partial tree would flicker between rendered and raw.
 */
export function stripOpenUIFences(raw: string, isStreaming = false): string {
  if (!raw) return "";
  let text = raw.trimStart();
  const openFenceMatch = /^```[a-zA-Z0-9_-]*\r?\n/.exec(text);
  if (openFenceMatch) {
    text = text.slice(openFenceMatch[0].length);
    if (!isStreaming || /\r?\n```\s*$/.test(text)) {
      text = text.replace(/\r?\n```\s*$/, "");
    }
  }
  return text;
}

/**
 * True when the text is an openui-lang program rather than prose or a code block.
 *
 * Rejects outer fences with a non-OpenUI language tag (e.g. ```python), ignores
 * any `root = ...` inside nested markdown fences, requires a PascalCase
 * component call (`root = Stack(...)`), and rejects prose lines before `root`.
 */
export function hasOpenUILangSyntax(raw: string | null | undefined): boolean {
  if (!raw) return false;
  const trimmed = raw.trimStart();
  const openFenceMatch = /^```([a-zA-Z0-9_-]*)\r?\n/.exec(trimmed);
  if (openFenceMatch) {
    const lang = openFenceMatch[1] ?? "";
    if (lang !== "" && !/^openui(?:[-_]?lang)?$/i.test(lang)) {
      return false;
    }
  }

  const cleaned = stripOpenUIFences(raw, false);
  let inFence = false;
  for (const line of cleaned.split(/\r?\n/)) {
    const stripped = line.trim();
    if (stripped.startsWith("```")) {
      inFence = !inFence;
      continue;
    }
    if (inFence || stripped === "" || stripped.startsWith("#")) {
      continue;
    }
    if (/^root\s*=\s*[A-Z][A-Za-z0-9_]*\s*\(/.test(stripped)) {
      return true;
    }
    if (!/^[a-zA-Z_][a-zA-Z0-9_]*\s*=/.test(stripped)) {
      return false;
    }
  }
  return false;
}
