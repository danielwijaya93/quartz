import * as React from "react";
import { Button } from "@heroui/react";
import { Plug, Settings } from "lucide-react";
import { ThemeToggle } from "../ThemeToggle";

export function SidebarFooter({
  drawer,
  onClose,
  onOpenMcp,
  onOpenSettings,
}: {
  drawer: boolean;
  onClose: () => void;
  onOpenMcp?: () => void;
  onOpenSettings?: () => void;
}): React.ReactElement {
  return (
    <div className="flex shrink-0 items-center justify-between border-t border-separator/70 px-2 py-1.5">
      <ThemeToggle />
      <div className="flex items-center gap-0.5">
        {onOpenMcp ? (
          <Button
            isIconOnly
            size="sm"
            variant="ghost"
            aria-label="Open MCP servers"
            onPress={() => {
              onOpenMcp();
              if (drawer) onClose();
            }}
            className="size-6 min-w-6 text-muted hover:text-foreground"
          >
            <Plug aria-hidden="true" className="size-3.5" />
          </Button>
        ) : null}
        {onOpenSettings ? (
          <Button
            isIconOnly
            size="sm"
            variant="ghost"
            aria-label="Open settings"
            onPress={() => {
              onOpenSettings();
              if (drawer) onClose();
            }}
            className="size-6 min-w-6 text-muted hover:text-foreground"
          >
            <Settings aria-hidden="true" className="size-3.5" />
          </Button>
        ) : null}
      </div>
    </div>
  );
}
