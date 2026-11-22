import * as React from "react";
import { Switch } from "@heroui/react";
import type { RichUiMode } from "@/lib/richUi";

/**
 * Rich UI on/off toggle.
 *
 * Off by default (Markdown). Turning Rich UI on appends the OpenUI Lang system
 * prompt to the session so the model emits interactive charts, tables, and forms.
 */
export function RichUiToggle({
  mode,
  onChange,
}: {
  mode: RichUiMode;
  onChange: (mode: RichUiMode) => void;
}): React.ReactElement {
  const isRich = mode === "rich";

  return (
    <Switch
      size="sm"
      isSelected={isRich}
      onChange={(selected) => onChange(selected ? "rich" : "markdown")}
      aria-label="Rich UI"
      className="flex-row items-center gap-1.5"
    >
      <Switch.Control>
        <Switch.Thumb />
      </Switch.Control>
      <Switch.Content className="whitespace-nowrap text-xs font-medium text-muted select-none">
        Rich UI
      </Switch.Content>
    </Switch>
  );
}
