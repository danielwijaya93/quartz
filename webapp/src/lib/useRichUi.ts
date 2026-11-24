import * as React from "react";
import { authFetch } from "./authToken";
import { apiUrl } from "./serverUrl";
import {
  readRichUiMode,
  wantsRichUi,
  writeRichUiMode,
  type RichUiMode,
} from "./richUi";

export interface RichUiController {
  mode: RichUiMode;
  /** Persist a new mode locally and sync it to the server. */
  setMode: (mode: RichUiMode) => void;
  /** Flip between the two modes and sync to the server. */
  toggle: () => void;
  /** Apply a server-synced mode without re-posting back to the server. */
  applySyncedMode: (mode: RichUiMode) => void;
}

function persistRichUiToServer(mode: RichUiMode): void {
  void authFetch(apiUrl("/api/settings"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key: "rich_ui", value: mode === "rich" ? "on" : "off" }),
  }).catch(() => {
    /* Offline or test environment: localStorage still holds the choice. */
  });
}

/**
 * The Rich UI preference, read once at mount and written on change.
 *
 * Read synchronously in a lazy initialiser so the very first `hello` already
 * knows whether to carry the OpenUI prompt: deferring it to an effect would send
 * one session-building frame with the wrong configuration.
 */
export function useRichUiMode(): RichUiController {
  const [mode, setModeState] = React.useState<RichUiMode>(() => readRichUiMode());

  const applySyncedMode = React.useCallback((next: RichUiMode) => {
    setModeState(next);
    writeRichUiMode(next);
  }, []);

  const setMode = React.useCallback((next: RichUiMode) => {
    setModeState(next);
    writeRichUiMode(next);
    persistRichUiToServer(next);
  }, []);

  const toggle = React.useCallback(() => {
    setModeState((current) => {
      const next: RichUiMode = current === "rich" ? "markdown" : "rich";
      writeRichUiMode(next);
      persistRichUiToServer(next);
      return next;
    });
  }, []);

  return { mode, setMode, toggle, applySyncedMode };
}

/**
 * The OpenUI Lang system prompt for the current preference.
 *
 * Empty when Rich UI is off -- that empty string *is* how the prompt is withdrawn
 * from the session, so the model is never told to emit openui-lang for a client
 * that will not draw it. When on, the prompt module is imported on demand: it
 * pulls in the compiled spec (`src/generated/spec.json`, ~140 kB) and the lang-core
 * generator, and a Markdown-only reader should not download either.
 */
export function useOpenUISystemPrompt(mode: RichUiMode): string {
  const [prompt, setPrompt] = React.useState("");

  React.useEffect(() => {
    if (!wantsRichUi(mode)) {
      setPrompt("");
      return;
    }
    let cancelled = false;
    void import("./openui/prompt")
      .then((module) => {
        if (!cancelled) setPrompt(module.getOpenUISystemPrompt());
      })
      .catch(() => {
        // The chunk failed to load. Rich UI then renders markdown, which is a
        // visible degradation rather than a broken composer.
        if (!cancelled) setPrompt("");
      });
    return () => {
      cancelled = true;
    };
  }, [mode]);

  return prompt;
}
