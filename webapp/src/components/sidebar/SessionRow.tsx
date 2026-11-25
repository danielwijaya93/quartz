import * as React from "react";
import { Button, cn } from "@heroui/react";
import { Columns2, Pencil, X } from "lucide-react";
import type { TileBadge } from "@/lib/tiling";
import {
  displayTitle,
  formatAge,
  type SessionActivity,
  type SidebarSession,
  type SidebarState,
} from "@/lib/sidebar";
import { SessionStatusDot } from "./SessionStatusDot";

export function SessionRow({
  session,
  sidebar,
  activity,
  folderLabel,
  current,
  tileBadge,
  canOpenInNewTile = false,
  onSelect,
  onOpenInNewTile,
  onRename,
  onHide,
}: {
  session: SidebarSession;
  sidebar: SidebarState;
  activity?: SessionActivity;
  folderLabel?: string;
  current: boolean;
  tileBadge?: TileBadge;
  canOpenInNewTile?: boolean;
  onSelect: () => void;
  onOpenInNewTile?: () => void;
  onRename: (title: string) => void;
  onHide: () => void;
}): React.ReactElement {
  const [renaming, setRenaming] = React.useState(false);
  const [draft, setDraft] = React.useState("");
  const inputId = React.useId();
  const inputRef = React.useRef<HTMLInputElement | null>(null);
  const title = displayTitle(session, sidebar);
  const age = formatAge(session.updatedAt);

  React.useEffect(() => {
    if (!renaming) return;
    inputRef.current?.focus();
    inputRef.current?.select();
  }, [renaming]);

  const startRename = (): void => {
    setDraft(title);
    setRenaming(true);
  };

  const commit = (): void => {
    setRenaming(false);
    const next = draft.trim();
    // An empty draft clears the rename server-side, which is the only way back to
    // the preview title without a terminal.
    if (next !== title) onRename(next);
  };

  return (
    <li className="relative">
      {renaming ? (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            commit();
          }}
          className="flex h-7 min-h-7 items-center gap-1 rounded-md bg-surface-secondary px-1.5"
        >
          <label htmlFor={inputId} className="sr-only">
            Session title
          </label>
          <input
            id={inputId}
            ref={inputRef}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.stopPropagation();
                setRenaming(false);
              }
            }}
            onBlur={commit}
            className="min-w-0 flex-1 rounded border border-focus bg-background px-1.5 py-0.5 text-[12px] text-foreground outline-none"
          />
        </form>
      ) : (
        <div
          className={cn(
            "group/session relative flex h-7 min-h-7 items-center gap-1 rounded-md px-1.5 transition-colors",
            current
              ? "bg-accent-soft font-medium text-accent-soft-foreground"
              : "text-muted hover:text-foreground hover:bg-surface-secondary/60",
          )}
        >
          <button
            type="button"
            onClick={(event) => {
              if ((event.metaKey || event.ctrlKey) && onOpenInNewTile) {
                event.preventDefault();
                onOpenInNewTile();
                return;
              }
              onSelect();
            }}
            aria-current={current ? "true" : undefined}
            className="flex h-7 min-h-7 min-w-0 flex-1 cursor-pointer items-center truncate text-left text-[12px] focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-focus"
          >
            <SessionStatusDot activity={activity} />
            <span
              className={cn(
                "truncate",
                activity?.unread && !current && "font-semibold text-foreground",
              )}
              title={`${title}${folderLabel ? ` · ${folderLabel}` : ""}${age ? ` · ${age} old` : ""}`}
            >
              {title}
            </span>
            {folderLabel ? (
              <span className="ml-1 max-w-[34%] shrink-0 truncate text-[10px] text-meta">
                · {folderLabel}
              </span>
            ) : null}
          </button>

          {tileBadge ? (
            <span
              aria-label={`Open in tile ${tileBadge.number}`}
              className={cn(
                "inline-flex size-4 shrink-0 items-center justify-center rounded text-[10px] font-bold tabular-nums group-hover/session:hidden group-focus-within/session:hidden",
                tileBadge.focused
                  ? "bg-sky-500 text-zinc-950"
                  : "border border-border bg-surface-secondary text-foreground",
              )}
            >
              {tileBadge.number}
            </span>
          ) : age ? (
            <span
              aria-hidden="true"
              className="shrink-0 text-[10px] text-meta tabular-nums group-hover/session:hidden group-focus-within/session:hidden"
            >
              {age}
            </span>
          ) : null}

          <div
            className="flex shrink-0 items-center gap-0.5 motion-safe:transition-opacity [@media(hover:hover)]:opacity-0 group-hover/session:opacity-100 group-focus-within/session:opacity-100"
          >
            {onOpenInNewTile && !tileBadge ? (
              <Button
                isIconOnly
                size="sm"
                variant="ghost"
                isDisabled={!canOpenInNewTile}
                aria-label={canOpenInNewTile ? `Open ${title} in new tile` : "Max 4 tiles open"}
                onPress={onOpenInNewTile}
                className="size-5 min-w-5 h-5 w-5 rounded p-0 text-muted hover:text-foreground hover:bg-surface-secondary"
              >
                <Columns2 aria-hidden="true" className="size-2.5" />
              </Button>
            ) : null}
            <Button
              isIconOnly
              size="sm"
              variant="ghost"
              aria-label={`Rename session ${title}`}
              onPress={startRename}
              className="size-5 min-w-5 h-5 w-5 rounded p-0 text-muted hover:text-foreground hover:bg-surface-secondary"
            >
              <Pencil aria-hidden="true" className="size-2.5" />
            </Button>
            <Button
              isIconOnly
              size="sm"
              variant="ghost"
              aria-label={`Hide session ${title} from the sidebar`}
              onPress={onHide}
              className="size-5 min-w-5 h-5 w-5 rounded p-0 text-muted hover:text-foreground hover:bg-surface-secondary"
            >
              <X aria-hidden="true" className="size-2.5" />
            </Button>
          </div>
        </div>
      )}
    </li>
  );
}
