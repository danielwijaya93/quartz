/**
 * The response-format preference: markdown (default) or dashboard.
 *
 * This is a *client* preference, persisted like the theme and sent per prompt as
 * `richUi` on the `prompt` frame. It is not session state: the server does not
 * remember it, and a new tab starts from whatever the user last chose.
 *
 * Pure functions plus one thin hook, so the storage rules are testable without a
 * DOM and the component tree has a single source of truth.
 */

import * as React from "react";

export type RichUiMode = "markdown" | "dashboard";

export const RICH_UI_STORAGE_KEY = "quartz-rich-ui";

export const RICH_UI_MODES: readonly RichUiMode[] = ["markdown", "dashboard"];

export const DEFAULT_RICH_UI_MODE: RichUiMode = "markdown";

export function isRichUiMode(value: unknown): value is RichUiMode {
  return value === "markdown" || value === "dashboard";
}

/** Narrow a raw storage read. Anything unrecognised falls back to the default. */
export function parseRichUiMode(value: string | null | undefined): RichUiMode {
  return isRichUiMode(value) ? value : DEFAULT_RICH_UI_MODE;
}

type StorageLike = Pick<Storage, "getItem" | "setItem">;

function storage(): StorageLike | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    // A blocked storage API (private mode, embedded webview) must not break the
    // app: the preference simply does not persist.
    return null;
  }
}

/** Read the stored preference, defaulting to markdown. */
export function readRichUiMode(target: StorageLike | null = storage()): RichUiMode {
  if (target === null) return DEFAULT_RICH_UI_MODE;
  try {
    return parseRichUiMode(target.getItem(RICH_UI_STORAGE_KEY));
  } catch {
    return DEFAULT_RICH_UI_MODE;
  }
}

/** Persist the preference. A failure to store is not a failure to switch. */
export function writeRichUiMode(mode: RichUiMode, target: StorageLike | null = storage()): void {
  if (target === null) return;
  try {
    target.setItem(RICH_UI_STORAGE_KEY, mode);
  } catch {
    // Ignored on purpose; the in-memory state is already correct.
  }
}

export interface RichUiController {
  mode: RichUiMode;
  setMode: (mode: RichUiMode) => void;
  /** True when the next prompt should ask the server for a surface. */
  dashboard: boolean;
}

/**
 * The app's single instance of the preference.
 *
 * Deliberately *not* a global store: the owner is `App`, which passes it to the
 * header control and to `useQuartz.prompt`. One owner, one value, no drift.
 */
export function useRichUiMode(): RichUiController {
  const [mode, setStoredMode] = React.useState<RichUiMode>(() => readRichUiMode());

  const setMode = React.useCallback((next: RichUiMode) => {
    setStoredMode(next);
    writeRichUiMode(next);
  }, []);

  return { mode, setMode, dashboard: mode === "dashboard" };
}
