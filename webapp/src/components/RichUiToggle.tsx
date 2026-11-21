import * as React from "react";
import { ToggleButton, ToggleButtonGroup } from "@heroui/react";
import { FileText, LayoutDashboard } from "lucide-react";
import { RICH_UI_MODES, type RichUiMode } from "@/lib/richUi";

const MODE_COPY: Record<RichUiMode, { label: string; icon: React.ReactElement }> = {
  markdown: { label: "Markdown", icon: <FileText aria-hidden="true" className="size-3.5" /> },
  dashboard: {
    label: "Dashboard",
    icon: <LayoutDashboard aria-hidden="true" className="size-3.5" />,
  },
};

/**
 * Response-format control: Markdown (default) or Dashboard.
 *
 * Two explicit labelled options rather than a switch, for the same reason the
 * theme control is a group: the user can see both states, and the active one is
 * announced as checked rather than signalled by colour alone. The label text is
 * always visible -- an icon-only toggle would make the two states guesswork.
 */
export function RichUiToggle({
  mode,
  onChange,
}: {
  mode: RichUiMode;
  onChange: (mode: RichUiMode) => void;
}): React.ReactElement {
  const onSelectionChange = React.useCallback(
    (keys: "all" | Iterable<React.Key>) => {
      if (keys === "all") return;
      const [first] = [...keys];
      const next = RICH_UI_MODES.find((entry) => entry === first);
      if (next) onChange(next);
    },
    [onChange],
  );

  return (
    <ToggleButtonGroup
      aria-label="Response format"
      size="sm"
      isDetached
      disallowEmptySelection
      selectionMode="single"
      selectedKeys={[mode]}
      onSelectionChange={onSelectionChange}
    >
      {RICH_UI_MODES.map((entry) => (
        <ToggleButton
          key={entry}
          id={entry}
          aria-label={`${MODE_COPY[entry].label} response`}
          className="cursor-pointer gap-1.5 px-2.5 text-xs font-medium"
        >
          {MODE_COPY[entry].icon}
          <span>{MODE_COPY[entry].label}</span>
        </ToggleButton>
      ))}
    </ToggleButtonGroup>
  );
}
