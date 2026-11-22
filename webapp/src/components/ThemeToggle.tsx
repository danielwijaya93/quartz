import * as React from "react";
import { ToggleButton, ToggleButtonGroup } from "@heroui/react";
import { Monitor, Moon, Sun } from "lucide-react";
import { THEME_MODES, type ThemeMode, useThemeMode } from "@/lib/theme";

const MODE_COPY: Record<ThemeMode, { label: string; icon: React.ReactElement }> = {
  light: { label: "Light", icon: <Sun aria-hidden="true" className="size-3.5" /> },
  dark: { label: "Dark", icon: <Moon aria-hidden="true" className="size-3.5" /> },
  system: { label: "System", icon: <Monitor aria-hidden="true" className="size-3.5" /> },
};

/**
 * Theme control.
 *
 * Three explicit states rather than one button that cycles: the user can see
 * which mode is active *and* what the other two are without pressing anything.
 * Each option is a labelled toggle button, so the state is announced as pressed
 * or not pressed and never relies on colour alone.
 */
export function ThemeToggle(): React.ReactElement {
  const { mode, setMode } = useThemeMode();

  const onSelectionChange = React.useCallback(
    (keys: "all" | Iterable<React.Key>) => {
      if (keys === "all") return;
      const [first] = [...keys];
      const next = THEME_MODES.find((entry) => entry === first);
      if (next) setMode(next);
    },
    [setMode],
  );

  return (
    <ToggleButtonGroup
      aria-label="Colour theme"
      size="sm"
      disallowEmptySelection
      selectionMode="single"
      selectedKeys={[mode]}
      onSelectionChange={onSelectionChange}
      className="rounded-lg border border-separator bg-surface-secondary/60 p-0.5"
    >
      {THEME_MODES.map((entry) => (
        <ToggleButton
          key={entry}
          id={entry}
          aria-label={`${MODE_COPY[entry].label} theme`}
          className="h-7 cursor-pointer gap-1.5 rounded-md px-2 text-xs font-medium"
        >
          {MODE_COPY[entry].icon}
          <span className="hidden xl:inline">{MODE_COPY[entry].label}</span>
        </ToggleButton>
      ))}
    </ToggleButtonGroup>
  );
}
