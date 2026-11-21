import * as React from "react";
import { ToggleButton, ToggleButtonGroup } from "@heroui/react";
import { LayoutGrid, TextAlignStart } from "lucide-react";
import { RICH_UI_MODES, type RichUiMode } from "@/lib/richUi";

const MODE_COPY: Record<
  RichUiMode,
  { label: string; title: string; icon: React.ReactElement }
> = {
  markdown: {
    label: "Markdown",
    title: "Draw replies as markdown. The model is not asked for OpenUI Lang.",
    icon: <TextAlignStart aria-hidden="true" className="size-3.5" />,
  },
  rich: {
    label: "Rich UI",
    title: "Draw replies as OpenUI charts, tables and forms.",
    icon: <LayoutGrid aria-hidden="true" className="size-3.5" />,
  },
};

/**
 * Markdown / Rich UI control.
 *
 * Two explicit options rather than one switch: the user can see which mode is on
 * *and* what the other one is without pressing anything. Every option carries both
 * an icon and its label, so the state is never conveyed by colour alone, and each
 * is a real `aria-pressed` toggle for assistive tech.
 *
 * It is not a view-only filter: turning Rich UI off withdraws the OpenUI Lang
 * system prompt from the session, so the model stops emitting openui-lang and the
 * reply arrives as ordinary markdown.
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
      aria-label="Assistant reply format"
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
          aria-label={`${MODE_COPY[entry].label} replies: ${MODE_COPY[entry].title}`}
          className="cursor-pointer gap-1.5 px-2.5 text-xs font-medium"
        >
          {MODE_COPY[entry].icon}
          <span className="hidden sm:inline">{MODE_COPY[entry].label}</span>
        </ToggleButton>
      ))}
    </ToggleButtonGroup>
  );
}