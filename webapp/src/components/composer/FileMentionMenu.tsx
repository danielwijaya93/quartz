import * as React from "react";
import { cn } from "@heroui/react";
import { CornerDownLeft, FileText } from "lucide-react";

export function FileMentionMenu({
  query,
  filesLoading,
  workspaceFilesCount,
  mentionFiles,
  activeFile,
  fileListRef,
  onHoverIndex,
  onSelectMention,
}: {
  query: string | undefined;
  filesLoading: boolean;
  workspaceFilesCount: number;
  mentionFiles: readonly string[];
  activeFile: number;
  fileListRef: React.RefObject<HTMLUListElement | null>;
  onHoverIndex: (index: number) => void;
  onSelectMention: (filePath: string) => void;
}): React.ReactElement {
  return (
    <div
      role="region"
      aria-label="Workspace files"
      className="absolute right-0 bottom-full left-0 z-20 mb-2 overflow-hidden rounded-xl border border-border bg-surface shadow-lg"
    >
      <div className="flex items-center justify-between border-b border-separator px-3 py-2 text-xs text-muted">
        <span className="font-medium text-foreground">Files</span>
        <span>↑↓ navigate · Tab/Enter insert · Esc close</span>
      </div>
      {filesLoading && workspaceFilesCount === 0 ? (
        <p className="px-3 py-4 text-center text-sm text-muted">Loading workspace files…</p>
      ) : mentionFiles.length === 0 ? (
        <p className="px-3 py-4 text-center text-sm text-muted">
          No file matches “@{query}”.
        </p>
      ) : (
        <ul
          ref={fileListRef}
          id="inline-file-list"
          role="listbox"
          aria-label="Matching workspace files"
          className="max-h-64 space-y-0.5 overflow-y-auto p-1.5"
        >
          {mentionFiles.map((filePath, index) => {
            const selected = index === activeFile;
            return (
              <li
                key={filePath}
                role="option"
                aria-selected={selected}
                onMouseEnter={() => onHoverIndex(index)}
                onMouseDown={(event) => {
                  event.preventDefault();
                  onSelectMention(filePath);
                }}
                className={cn(
                  "flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-xs transition-colors",
                  selected
                    ? "bg-accent-soft text-accent-soft-foreground"
                    : "text-foreground hover:bg-surface-secondary/70",
                )}
              >
                <FileText aria-hidden="true" className="size-3.5 shrink-0 text-muted" />
                <span className="min-w-0 flex-1 truncate font-mono">{filePath}</span>
                {selected ? (
                  <CornerDownLeft aria-hidden="true" className="size-3.5 shrink-0 text-muted" />
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
