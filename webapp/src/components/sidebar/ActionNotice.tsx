import * as React from "react";
import { Button, cn } from "@heroui/react";
import { Undo2, X } from "lucide-react";

/**
 * What the sidebar is confirming, and what undoing it would take.
 *
 * The action is kept whole rather than as a callback so the notice can offer the
 * same request twice: an undo that fails changes the sentence, not the affordance.
 */
export type NoticeAction =
  | { kind: "hide"; sessionId: string; title: string }
  | { kind: "rename"; sessionId: string; title: string; previousTitle: string };

export interface Notice {
  action: NoticeAction;
  /** Set when the undo itself failed. The notice stays on screen and says so. */
  error?: string;
}

function noticeMessage(action: NoticeAction): string {
  if (action.kind === "hide") return `Hidden “${action.title}”.`;
  return action.title === "" ? "Rename cleared." : `Renamed to “${action.title}”.`;
}

/**
 * An undoable confirmation: one sentence, one action, one way out.
 *
 * It is part of the sidebar rather than a floating toast because that is where the
 * change happened and because the sidebar column cannot cover the composer. It
 * takes no focus: a confirmation that moves the caret out of a half-written prompt
 * is worse than the row it replaced. Every control in it is a normal tab stop, so
 * the keyboard reaches it without a hover.
 */
export function ActionNotice({
  notice,
  busy,
  onUndo,
  onDismiss,
}: {
  notice: Notice;
  busy: boolean;
  onUndo: () => void;
  onDismiss: () => void;
}): React.ReactElement {
  const failed = notice.error !== undefined;

  return (
    <div
      className={cn(
        "flex items-center gap-1.5 rounded-md border px-2 py-1",
        failed ? "border-danger/40 bg-danger-soft" : "border-border bg-surface",
      )}
    >
      <p
        className={cn(
          "min-w-0 flex-1 text-[11px] leading-tight break-words",
          failed ? "text-danger-soft-foreground" : "text-foreground",
        )}
      >
        {notice.error ?? noticeMessage(notice.action)}
      </p>
      <Button
        size="sm"
        variant={failed ? "outline" : "ghost"}
        isDisabled={busy}
        onPress={onUndo}
        className="h-6 shrink-0 px-1.5 text-[11px]"
      >
        <Undo2 aria-hidden="true" className="size-3" />
        {failed ? "Try again" : "Undo"}
      </Button>
      <Button
        isIconOnly
        size="sm"
        variant="ghost"
        aria-label="Dismiss notification"
        onPress={onDismiss}
        className="size-5 min-w-5 shrink-0 p-0 text-muted hover:text-foreground"
      >
        <X aria-hidden="true" className="size-3" />
      </Button>
    </div>
  );
}
