import * as React from "react";
import { Markdown } from "../Markdown";
import { hasOpenUILangSyntax, stripOpenUIFences } from "@/lib/openui/lang-helpers";
import type { AssistantRow } from "@/lib/transcript";

/**
 * The renderer, in its own chunk.
 *
 * `@openuidev/react-lang` plus the whole component library is by far the largest
 * dependency in this app, and a reader who never turns Rich UI on should not pay
 * for it. `React.lazy` keeps it out of the initial bundle; the toggle is the only
 * thing that ever loads it.
 */
const OpenUISurface = React.lazy(() =>
  import("../OpenUISurface").then((module) => ({ default: module.OpenUISurface })),
);

/** Anything the renderer throws is shown, not swallowed: a blank box is worse. */
class SurfaceBoundary extends React.Component<
  { fallback: React.ReactNode; children: React.ReactNode },
  { failed: boolean }
> {
  override state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  override render(): React.ReactNode {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}

export interface AssistantRowViewProps {
  row: AssistantRow;
  streaming: boolean;
  /** Draw openui-lang replies as surfaces instead of prose. */
  richUi: boolean;
  /** Send a message into the session (an OpenUI form submit lands here). */
  onPrompt: (text: string) => void;
  /** Persist a surface's form state onto the row. */
  onFormState: (id: string, state: Record<string, unknown>) => void;
}

/**
 * The answer: full reading width, no card, no gutter - just prose or a surface.
 *
 * Two renderers, one decision: if Rich UI is on *and* the text is an openui-lang
 * program, it is drawn with OpenUI; otherwise it is markdown, exactly as before.
 * The syntax check runs on fence-stripped text, because a model that wraps its
 * program in ```openui-lang has still produced a surface.
 */
export function AssistantRowView({
  row,
  streaming,
  richUi,
  onPrompt,
  onFormState,
}: AssistantRowViewProps): React.ReactElement {
  const cleaned = React.useMemo(
    () => stripOpenUIFences(row.text, streaming),
    [row.text, streaming],
  );
  const isOpenUILang = richUi && hasOpenUILangSyntax(cleaned);

  const handleContinue = React.useCallback(
    (message: string) => {
      if (message.trim() === "") return;
      onPrompt(message);
    },
    [onPrompt],
  );

  const handleOpenUrl = React.useCallback((url: string) => {
    if (url === "") return;
    window.open(url, "_blank", "noopener,noreferrer");
  }, []);

  const handleStateUpdate = React.useCallback(
    (state: Record<string, unknown>) => {
      onFormState(row.id, state);
    },
    [onFormState, row.id],
  );

  const markdown = <Markdown streaming={streaming}>{row.text}</Markdown>;

  return (
    <article className="flex flex-col gap-1.5">
      <p className="flex items-center gap-1.5 text-xs font-medium text-muted">
        <span aria-hidden="true" className="size-1.5 rounded-full bg-accent" />
        Quartz
      </p>
      {isOpenUILang ? (
        <SurfaceBoundary fallback={markdown}>
          <React.Suspense fallback={<SurfacePending />}>
            <OpenUISurface
              response={cleaned}
              isStreaming={streaming}
              initialState={row.formState ?? undefined}
              onContinueConversation={handleContinue}
              onOpenUrl={handleOpenUrl}
              onStateUpdate={handleStateUpdate}
            />
          </React.Suspense>
        </SurfaceBoundary>
      ) : (
        markdown
      )}
    </article>
  );
}

/** The renderer chunk is arriving. Text, not a spinner: it resolves in one frame. */
function SurfacePending(): React.ReactElement {
  return (
    <p role="status" aria-busy="true" className="text-sm text-muted">
      Drawing interface…
    </p>
  );
}
