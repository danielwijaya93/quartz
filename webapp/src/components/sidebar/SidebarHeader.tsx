import * as React from "react";
import { Button, cn } from "@heroui/react";
import { ArchiveRestore, FolderPlus, RefreshCw, Search, X } from "lucide-react";

export function SidebarHeader({
  drawer,
  searchQuery,
  unreadOnly,
  unreadCount,
  onNewFolder,
  onReload,
  onOpenArchive,
  onClose,
  onSearchChange,
  onToggleUnreadOnly,
}: {
  drawer: boolean;
  searchQuery: string;
  unreadOnly: boolean;
  unreadCount: number;
  onNewFolder: () => void;
  onReload: () => void;
  onOpenArchive: () => void;
  onClose: () => void;
  onSearchChange: (value: string) => void;
  onToggleUnreadOnly: () => void;
}): React.ReactElement {
  return (
    <>
      <div className="flex h-9 shrink-0 items-center gap-0.5 border-b border-separator/70 px-2">
        <h2 className="min-w-0 flex-1 truncate px-1 text-[11px] font-semibold uppercase tracking-wider text-muted">
          Workspaces
        </h2>
        <Button
          isIconOnly
          size="sm"
          variant="ghost"
          aria-label="Add a folder to the sidebar"
          onPress={onNewFolder}
          className="size-6 min-w-6 text-muted hover:text-foreground"
        >
          <FolderPlus aria-hidden="true" className="size-3.5" />
        </Button>
        <Button
          isIconOnly
          size="sm"
          variant="ghost"
          aria-label="Refresh sessions"
          onPress={onReload}
          className="size-6 min-w-6 text-muted hover:text-foreground"
        >
          <RefreshCw aria-hidden="true" className="size-3.5" />
        </Button>
        <Button
          isIconOnly
          size="sm"
          variant="ghost"
          aria-label="Open the session archive"
          onPress={onOpenArchive}
          className="size-6 min-w-6 text-muted hover:text-foreground"
        >
          <ArchiveRestore aria-hidden="true" className="size-3.5" />
        </Button>
        {drawer ? (
          <Button
            isIconOnly
            size="sm"
            variant="ghost"
            aria-label="Close the session sidebar"
            onPress={onClose}
            className="size-6 min-w-6 text-muted hover:text-foreground"
          >
            <X aria-hidden="true" className="size-3.5" />
          </Button>
        ) : null}
      </div>

      <div className="shrink-0 border-b border-separator/50 px-2 py-1.5">
        <div className="flex items-center gap-1.5">
          <div className="relative flex min-w-0 flex-1 items-center">
            <Search
              aria-hidden="true"
              className="pointer-events-none absolute left-2 size-3.5 text-muted"
            />
            <input
              type="search"
              value={searchQuery}
              onChange={(event) => onSearchChange(event.target.value)}
              placeholder="Filter sessions…"
              aria-label="Filter sessions and folders"
              className="h-7 w-full rounded-md border border-border/60 bg-surface-secondary/50 pl-7 pr-6 text-xs text-foreground placeholder:text-muted/80 focus:border-focus focus:bg-background focus:outline-none"
            />
            {searchQuery !== "" ? (
              <button
                type="button"
                onClick={() => onSearchChange("")}
                aria-label="Clear filter"
                className="absolute right-1.5 inline-flex size-4 cursor-pointer items-center justify-center rounded text-muted hover:text-foreground"
              >
                <X aria-hidden="true" className="size-3" />
              </button>
            ) : null}
          </div>
          <button
            type="button"
            aria-pressed={unreadOnly}
            onClick={onToggleUnreadOnly}
            className={cn(
              "inline-flex h-7 shrink-0 cursor-pointer items-center gap-1 rounded-md border px-2 text-[11px] font-medium transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-focus",
              unreadOnly
                ? "border-accent/50 bg-accent-soft text-accent-soft-foreground"
                : "border-border/60 bg-surface-secondary/40 text-muted hover:bg-surface-secondary/80 hover:text-foreground",
            )}
          >
            <span>Unread</span>
            {unreadCount > 0 ? (
              <span
                className={cn(
                  "rounded-full px-1 text-[10px] tabular-nums",
                  unreadOnly
                    ? "bg-accent/20 text-accent-soft-foreground"
                    : "bg-surface-secondary text-foreground/80",
                )}
              >
                {unreadCount}
              </span>
            ) : null}
          </button>
        </div>
      </div>
    </>
  );
}
