import * as React from "react";
import { ListBox, ListBoxItem, Select, cn } from "@heroui/react";

const EMPTY_KEY = "__quartz_empty__";

export interface QuartzSelectOption {
  value: string;
  label: string;
}

export interface QuartzSelectProps {
  id?: string;
  "aria-label"?: string;
  "aria-labelledby"?: string;
  value: string;
  options: readonly QuartzSelectOption[];
  onChange: (value: string) => void;
  disabled?: boolean;
  className?: string;
  triggerClassName?: string;
  placeholder?: string;
}

export function QuartzSelect({
  id,
  "aria-label": ariaLabel,
  "aria-labelledby": ariaLabelledBy,
  value,
  options,
  onChange,
  disabled = false,
  className,
  triggerClassName,
  placeholder,
}: QuartzSelectProps): React.ReactElement {
  const toKey = (v: string): string => (v === "" ? EMPTY_KEY : v);
  const fromKey = (k: string): string => (k === EMPTY_KEY ? "" : k);

  return (
    <Select
      id={id}
      aria-label={ariaLabel}
      aria-labelledby={ariaLabelledBy}
      selectedKey={toKey(value)}
      onSelectionChange={(key) => {
        if (key !== null) {
          onChange(fromKey(String(key)));
        }
      }}
      isDisabled={disabled}
      placeholder={placeholder}
      variant="secondary"
      className={cn("min-w-0", className)}
    >
      <Select.Trigger
        className={cn(
          "h-9 w-full rounded-lg border border-border bg-surface px-2.5 text-xs text-foreground shadow-none",
          triggerClassName,
        )}
      >
        <Select.Value className="truncate text-foreground" />
        <Select.Indicator className="text-muted" />
      </Select.Trigger>
      <Select.Popover className="z-50 min-w-[var(--trigger-width)] rounded-xl border border-border bg-surface p-1 shadow-lg">
        <ListBox className="max-h-60 overflow-y-auto outline-none">
          {options.map((opt) => (
            <ListBoxItem
              key={toKey(opt.value)}
              id={toKey(opt.value)}
              textValue={opt.label}
              className="cursor-pointer rounded-lg px-2.5 py-1.5 text-xs text-foreground outline-none data-[focused]:bg-surface-secondary data-[hovered]:bg-surface-secondary data-[selected]:bg-foreground data-[selected]:font-medium data-[selected]:text-background"
            >
              {opt.label}
            </ListBoxItem>
          ))}
        </ListBox>
      </Select.Popover>
    </Select>
  );
}
