import * as React from "react";
import { Alert } from "@heroui/react";
import { LayoutDashboard } from "lucide-react";
import { A2uiSurface, MarkdownComponentContext, type MarkdownComponentProps } from "@a2ui/react";
import { Markdown } from "../Markdown";
import { useA2uiSurfaceSet } from "@/lib/a2ui/useA2uiSurfaceSet";
import type { A2uiIssue } from "@/lib/a2ui/processor";
import type { A2uiRow } from "@/lib/transcript";

/**
 * Basic-catalog `Text` renders Markdown. The app already owns a Markdown
 * renderer, so A2UI text rides the same prose styles and theme tokens instead of
 * shipping a second typographic stack (and instead of the renderer's default
 * "no markdown renderer" console warning).
 */
function A2uiMarkdown({ markdown }: MarkdownComponentProps): React.ReactElement {
  return <Markdown className="text-sm">{markdown}</Markdown>;
}

/**
 * A renderer throw must not take the transcript down with it.
 *
 * React has no hook form for this, so it is the one class in the app: it exists
 * purely to turn "the surface component threw" into a sentence a user can act on.
 */
class A2uiBoundary extends React.Component<
  { children: React.ReactNode },
  { message: string | null }
> {
  override state: { message: string | null } = { message: null };

  static getDerivedStateFromError(error: unknown): { message: string } {
    return { message: error instanceof Error ? error.message : String(error) };
  }

  override render(): React.ReactNode {
    if (this.state.message !== null) {
      return (
        <A2uiFallback
          title="This dashboard could not be drawn"
          detail="The UI renderer failed on this surface. The rest of the conversation is unaffected."
          problems={[this.state.message]}
        />
      );
    }
    return this.props.children;
  }
}

/**
 * What the user sees instead of a blank rectangle.
 *
 * A surface with no components, a rejected envelope, and a renderer exception are
 * three different failures with three different fixes, so they get three
 * different sentences - but all of them say *something*, because an empty area
 * with no explanation is the one outcome that cannot be debugged.
 */
function A2uiFallback({
  title,
  detail,
  problems,
}: {
  title: string;
  detail: string;
  problems: readonly string[];
}): React.ReactElement {
  return (
    <Alert status="warning" role="group" className="rounded-xl">
      <Alert.Indicator />
      <Alert.Content>
        <Alert.Title>{title}</Alert.Title>
        <Alert.Description>
          <span className="text-sm">{detail}</span>
          {problems.length > 0 ? (
            <ul className="mt-1.5 space-y-0.5 font-mono text-xs text-muted">
              {problems.map((problem, index) => (
                <li key={`${problem}-${index}`}>{problem}</li>
              ))}
            </ul>
          ) : null}
        </Alert.Description>
      </Alert.Content>
    </Alert>
  );
}

function issueLines(issues: readonly A2uiIssue[]): string[] {
  return issues.map((issue) => (issue.path ? `${issue.path}: ${issue.message}` : issue.message));
}

/**
 * One A2UI surface, as it arrived in the transcript.
 *
 * The wrapper names the row so the surface is not mistaken for prose the model
 * wrote, and supplies the theme scope (`a2ui-scope`) that maps A2UI's CSS
 * variables onto HeroUI's tokens. It draws no panel of its own: the catalog's
 * `Card` is the only chrome, which is what keeps this row swappable when the
 * Quartz catalog lands.
 */
export function A2uiRowView({ row }: { row: A2uiRow }): React.ReactElement {
  const { surfaces, issues } = useA2uiSurfaceSet(row.messages);
  // A surface without its root component draws nothing: `createSurface` succeeded
  // but the `updateComponents` that fills it was rejected. Treat it as "not drawn"
  // rather than handing the renderer an empty div.
  const drawable = surfaces.filter(
    (surface) => surface.componentsModel.get(surface.rootId) !== undefined,
  );

  return (
    <section aria-label="Dashboard" className="a2ui-scope flex flex-col gap-2">
      <p className="flex items-center gap-1.5 text-xs font-medium text-muted">
        <LayoutDashboard aria-hidden="true" className="size-3.5" />
        Dashboard
        <span aria-hidden="true">·</span>
        <span className="font-normal">A2UI surface</span>
      </p>
      {drawable.length === 0 ? (
        <A2uiFallback
          title="No dashboard was drawn"
          detail={
            row.messages.length === 0
              ? "The server asked for a surface but sent no messages for it."
              : issues.length > 0
                ? "The renderer rejected part of this surface, so it could not be built."
                : "The renderer accepted the messages but produced no surface to show."
          }
          problems={issueLines(issues)}
        />
      ) : (
        <A2uiBoundary>
          {issues.length > 0 ? (
            <p className="text-xs text-warning">
              Part of this surface was rejected: {issueLines(issues).join("; ")}
            </p>
          ) : null}
          <MarkdownComponentContext.Provider value={A2uiMarkdown}>
            <div className="flex flex-col gap-2">
              {drawable.map((surface) => (
                <A2uiSurface key={surface.id} surface={surface} />
              ))}
            </div>
          </MarkdownComponentContext.Provider>
        </A2uiBoundary>
      )}
    </section>
  );
}
