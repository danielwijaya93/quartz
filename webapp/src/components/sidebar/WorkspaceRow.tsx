import * as React from "react";
import { Button, Disclosure, cn } from "@heroui/react";
import { ChevronRight, Plus, X } from "lucide-react";
import type { FolderBadge, WorkspaceGroup } from "@/lib/sidebar";

export function WorkspaceRow({
  group,
  current,
  badge,
  expanded,
  onToggle,
  onNewSession,
  onRemove,
  children,
}: {
  group: WorkspaceGroup;
  /** The live session runs here, so this is where the next prompt lands. */
  current: boolean;
  /** `new` only while this folder is waiting for its first session row. */
  badge: FolderBadge | null;
  expanded: boolean;
  onToggle: (expanded: boolean) => void;
  onNewSession: () => void;
  onRemove?: () => void;
  children: React.ReactNode;
}): React.ReactElement {
  const count = group.sessions.length;

  return (
    <Disclosure
      isExpanded={expanded}
      onExpandedChange={onToggle}
      className="rounded-md"
    >
      <div className="group/workspace flex items-center gap-0.5 rounded-md transition-colors hover:bg-surface-secondary/60">
        <Disclosure.Heading className="min-w-0 flex-1">
          <Disclosure.Trigger
            aria-label={`${group.label} folder`}
            aria-current={current ? "true" : undefined}
            className="flex h-7 min-h-7 w-full cursor-pointer items-center gap-1 rounded-md px-1.5 text-left focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-focus"
          >
            <ChevronRight
              aria-hidden="true"
              className={cn(
                "size-3 shrink-0 text-muted/70 motion-safe:transition-transform",
                expanded && "rotate-90",
              )}
            />
            <span
              className={cn(
                "min-w-0 truncate text-[12px]",
                current ? "font-semibold text-accent-soft-foreground" : "font-medium text-foreground/80",
              )}
              title={group.cwd}
            >
              {group.label}
            </span>
            {count > 0 ? (
              <span className="ml-auto shrink-0 rounded px-1 text-[10px] tabular-nums text-meta">
                {count}
              </span>
            ) : badge === null ? null : (
              <span className="ml-auto shrink-0 text-[10px] uppercase tracking-wide text-meta">
                {badge}
              </span>
            )}
          </Disclosure.Trigger>
        </Disclosure.Heading>
        {count === 0 && onRemove ? (
          <Button
            isIconOnly
            size="sm"
            variant="ghost"
            aria-label={`Remove ${group.label} from sidebar`}
            onPress={onRemove}
            className="size-5 min-w-5 h-5 w-5 shrink-0 rounded p-0 text-muted hover:text-danger hover:bg-surface-secondary motion-safe:transition-opacity [@media(hover:hover)]:opacity-0 group-hover/workspace:opacity-100 group-focus-within/workspace:opacity-100"
          >
            <X aria-hidden="true" className="size-3" />
          </Button>
        ) : null}
        <Button
          isIconOnly
          size="sm"
          variant="ghost"
          aria-label={`Start a new session in ${group.label}`}
          onPress={onNewSession}
          className="size-5 min-w-5 h-5 w-5 shrink-0 rounded p-0 text-muted hover:text-foreground hover:bg-surface-secondary motion-safe:transition-opacity [@media(hover:hover)]:opacity-0 group-hover/workspace:opacity-100 group-focus-within/workspace:opacity-100 mr-1"
        >
          <Plus aria-hidden="true" className="size-3" />
        </Button>
      </div>
      <Disclosure.Content>
        <Disclosure.Body className="mt-0.5 space-y-0.5 pl-2.5">
          {count === 0 ? (
            <p className="py-1 pl-2 text-[11px] text-muted">No sessions in this folder yet.</p>
          ) : (
            <ul className="space-y-0.5">{children}</ul>
          )}
        </Disclosure.Body>
      </Disclosure.Content>
    </Disclosure>
  );
}
