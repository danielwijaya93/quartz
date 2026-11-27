// The library's own stylesheet rides with this module instead of `index.css`, so a
// Markdown-only reader neither downloads ~700 kB of component CSS nor pays for it
// on the first paint. Vite emits it as a dependency of this lazy chunk.
import "@openuidev/react-ui/styles/index.css";

import * as React from "react";
import {
  BuiltinActionType,
  Renderer,
  buildMessage,
  type ActionEvent,
} from "@openuidev/react-lang";
import { openuiLibrary } from "@openuidev/react-ui/genui-lib";

/**
 * Draws one openui-lang program as a live surface.
 *
 * This module is the only place the OpenUI renderer, its component library and
 * its action vocabulary appear, and it is loaded on demand (see
 * `AssistantRowView`): a Markdown-only reader never downloads the renderer, and
 * the dependency stays swappable behind one file.
 *
 * Everything here is a *port* of the proven Antigravity integration minus its ACP
 * plumbing. Nothing outside needs to know what an `ActionEvent` is: the two
 * built-in action kinds are translated into plain callbacks here, so the caller
 * deals in "a message to send" and "a URL to open".
 *
 * The two behaviours worth naming:
 *
 *   - `@ToAssistant(...)` on a form button arrives as `ContinueConversation`. It
 *     becomes the text of a normal user prompt, so the model continues the same
 *     quartz session instead of starting a new one.
 *   - `onStateUpdate` fires on every field change. The caller persists it and
 *     hands it back as `initialState`, so a re-render -- a toggle, a resize, a
 *     delta elsewhere in the transcript -- does not wipe what the user typed.
 */

export interface OpenUISurfaceProps {
  /** The openui-lang program, fences already stripped. */
  response: string;
  /** True while deltas are still arriving; partial trees render, not a blank. */
  isStreaming: boolean;
  /** Previously persisted form state, if any. */
  initialState?: Record<string, unknown> | undefined;
  /** A `@ToAssistant` submit: send this as the next user turn. */
  onContinueConversation: (message: string) => void;
  /** An `OpenUrl` action: open this link. */
  onOpenUrl: (url: string) => void;
  /** Form state changed; persist it so it survives a re-render. */
  onStateUpdate: (state: Record<string, unknown>) => void;
}

/**
 * Turn a renderer action into the user message sent back to the model.
 *
 * The message carries the human-readable summary the button stated, plus the form
 * values and any action context as JSON -- which is what the model needs to answer
 * "what did they submit, and what was on screen when they did it". A button with
 * no label still needs a message, so the empty summary line is dropped rather than
 * sent as whitespace.
 */
export function buildActionUserMessage(event: ActionEvent): string {
  const messageCtx: unknown[] = [`User clicked: ${event.humanFriendlyMessage}`];
  const actionContext = event.params?.["context"];
  if (event.formState || actionContext !== undefined) {
    messageCtx.push(event.formState ?? {});
  }
  if (actionContext !== undefined) {
    messageCtx.push(actionContext);
  }
  const built = buildMessage({ content: event.humanFriendlyMessage, context: messageCtx });
  return event.humanFriendlyMessage ? built : built.slice(built.indexOf("\n") + 1);
}

/** Minimum interval between OpenUI full-program re-parses while tokens stream. */
const STREAM_THROTTLE_MS = 80;

export const OpenUISurface = React.memo(function OpenUISurface({
  response,
  isStreaming,
  initialState,
  onContinueConversation,
  onOpenUrl,
  onStateUpdate,
}: OpenUISurfaceProps): React.ReactElement {
  const [renderedResponse, setRenderedResponse] = React.useState(response);
  const lastFlushRef = React.useRef<number>(Date.now());
  const timerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  React.useEffect(() => {
    if (!isStreaming) {
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      lastFlushRef.current = Date.now();
      setRenderedResponse(response);
      return;
    }

    const elapsed = Date.now() - lastFlushRef.current;
    if (elapsed >= STREAM_THROTTLE_MS) {
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      lastFlushRef.current = Date.now();
      setRenderedResponse(response);
      return;
    }

    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
    }
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      lastFlushRef.current = Date.now();
      setRenderedResponse(response);
    }, STREAM_THROTTLE_MS - elapsed);

    return () => {
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [response, isStreaming]);

  const effectiveResponse = isStreaming ? renderedResponse : response;

  const handleAction = React.useCallback(
    (event: ActionEvent) => {
      if (event.type === BuiltinActionType.ContinueConversation) {
        onContinueConversation(buildActionUserMessage(event));
      } else if (event.type === BuiltinActionType.OpenUrl) {
        onOpenUrl(String(event.params?.["url"] ?? ""));
      }
    },
    [onContinueConversation, onOpenUrl],
  );

  return (
    <div className="openui-surface" data-testid="openui-surface">
      <Renderer
        response={effectiveResponse}
        library={openuiLibrary}
        isStreaming={isStreaming}
        onAction={handleAction}
        onStateUpdate={onStateUpdate}
        initialState={initialState}
      />
    </div>
  );
});