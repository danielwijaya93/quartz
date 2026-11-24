import * as React from "react";
import { Button } from "@heroui/react";
import { ArchiveRestore, FolderOpen } from "lucide-react";

/**
 * The whole-sidebar empty state.
 *
 * A first run has nothing to show and that is correct: every session that exists
 * was started in a terminal, and the sidebar lists the browser's own. Saying only
 * that would leave a person staring at an empty column, so the two ways to fill it
 * are right here -- open a folder and start something, or bring in what the
 * terminal already has.
 */
export function EmptySidebar({
  onOpenFolder,
  onOpenArchive,
}: {
  onOpenFolder: () => void;
  onOpenArchive: () => void;
}): React.ReactElement {
  return (
    <div className="rounded-xl border border-dashed border-border p-3 text-center">
      <p className="text-sm font-medium text-foreground">No sessions yet</p>
      <p className="mt-1 text-xs text-muted">
        Open a folder to start one, or import a session you began in the terminal.
      </p>
      <div className="mt-3 flex flex-col gap-1.5">
        <Button size="sm" variant="outline" onPress={onOpenFolder}>
          <FolderOpen aria-hidden="true" className="size-3.5" />
          Open a folder
        </Button>
        <Button size="sm" variant="outline" onPress={onOpenArchive}>
          <ArchiveRestore aria-hidden="true" className="size-3.5" />
          Import from terminal
        </Button>
      </div>
    </div>
  );
}
