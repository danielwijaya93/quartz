import * as React from "react";
import { Button, Tooltip } from "@heroui/react";
import { Folder, FolderPlus, Search, X } from "lucide-react";

export function SidebarHeader({
  searchQuery,
  onNewFolder,
  onSearchChange,
}: {
  searchQuery: string;
  onNewFolder: () => void;
  onSearchChange: (value: string) => void;
}): React.ReactElement {
  return (
    <>
      <div className="flex h-7 items-center gap-1.5 px-1.5">
        <Folder aria-hidden="true" className="size-3 shrink-0 text-muted" />
        <h2 className="min-w-0 flex-1 truncate text-[11px] font-semibold uppercase tracking-wider text-foreground/90">
          Workspaces
        </h2>
        <Tooltip delay={250}>
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
          <Tooltip.Content>Add a folder to the sidebar</Tooltip.Content>
        </Tooltip>
      </div>

      <div className="px-1 py-1.5">
        <div className="relative flex min-w-0 items-center">
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
      </div>
    </>
  );
}
