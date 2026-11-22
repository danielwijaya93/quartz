import * as React from "react";
import { Button } from "@heroui/react";
import { Monitor, Moon, Sun } from "lucide-react";
import { type ThemeMode, useThemeMode } from "@/lib/theme";

const MODE_COPY: Record<ThemeMode, { label: string; icon: React.ReactElement }> = {
  light: { label: "Light", icon: <Sun aria-hidden="true" className="size-4" /> },
  dark: { label: "Dark", icon: <Moon aria-hidden="true" className="size-4" /> },
  system: { label: "System", icon: <Monitor aria-hidden="true" className="size-4" /> },
};

const NEXT_MODE: Record<ThemeMode, ThemeMode> = {
  system: "light",
  light: "dark",
  dark: "system",
};

/**
 * Compact theme control.
 *
 * Cycles between System, Light, and Dark modes via a single icon button.
 */
export function ThemeToggle(): React.ReactElement {
  const { mode, setMode } = useThemeMode();

  const cycleMode = React.useCallback(() => {
    const next = NEXT_MODE[mode];
    setMode(next);
  }, [mode, setMode]);

  return (
    <Button
      isIconOnly
      size="sm"
      variant="ghost"
      onPress={cycleMode}
      aria-label={`Theme: ${MODE_COPY[mode].label}. Click to switch theme`}
      className="text-muted hover:text-foreground"
    >
      {MODE_COPY[mode].icon}
    </Button>
  );
}
