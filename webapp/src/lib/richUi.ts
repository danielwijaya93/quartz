/**
 * How assistant replies are drawn: Markdown (the default) or Rich UI.
 *
 * This is a *preference with a consequence*, not a view filter: with Rich UI on,
 * the client appends the OpenUI Lang system prompt to the session (see
 * `lib/openui/prompt.ts`) so the model actually emits a surface; with it off the
 * append is withdrawn, so the model is never told to emit openui-lang and the
 * prompt is not paid for. Rendering and prompting therefore read the same value.
 *
 * Three-state-safe, deliberately:
 *   - no stored value        -> "markdown"   (the cheap, always-correct default)
 *   - unreadable/blocked     -> "markdown"   (private mode, disabled storage)
 *   - unrecognised stored    -> "markdown"   (a value from a newer build)
 * `readRichUiMode` never throws and never returns a value outside the union, so
 * nothing downstream has to re-validate what localStorage said.
 */

export type RichUiMode = "markdown" | "rich";

/** Must match the key written by `writeRichUiMode`. */
export const RICH_UI_STORAGE_KEY = "quartz-rich-ui";

export const RICH_UI_MODES: readonly RichUiMode[] = ["markdown", "rich"] as const;

export const DEFAULT_RICH_UI_MODE: RichUiMode = "markdown";

export function isRichUiMode(value: unknown): value is RichUiMode {
  return value === "markdown" || value === "rich";
}

/** The stored preference, or the default when it is absent, unreadable or unknown. */
export function readRichUiMode(storage?: Pick<Storage, "getItem">): RichUiMode {
  const target = storage ?? (typeof localStorage === "undefined" ? undefined : localStorage);
  if (!target) return DEFAULT_RICH_UI_MODE;
  try {
    const stored = target.getItem(RICH_UI_STORAGE_KEY);
    return isRichUiMode(stored) ? stored : DEFAULT_RICH_UI_MODE;
  } catch {
    return DEFAULT_RICH_UI_MODE;
  }
}

/** Persist the preference. A blocked store is not an error: the app still works. */
export function writeRichUiMode(
  mode: RichUiMode,
  storage?: Pick<Storage, "setItem">,
): void {
  const target = storage ?? (typeof localStorage === "undefined" ? undefined : localStorage);
  if (!target) return;
  try {
    target.setItem(RICH_UI_STORAGE_KEY, mode);
  } catch {
    // Quota or private-mode rejection. The in-memory choice still applies.
  }
}

/** True when the model should be told to emit openui-lang. */
export function wantsRichUi(mode: RichUiMode): boolean {
  return mode === "rich";
}
