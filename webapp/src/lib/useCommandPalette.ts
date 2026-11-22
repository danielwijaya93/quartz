import * as React from "react";

export interface CommandPaletteControl {
  open: boolean;
  openPalette: () => void;
  onOpenChange: (open: boolean) => void;
}

/**
 * Open/close state for the command palette, plus the two rules that make it feel
 * like part of the app rather than an overlay bolted on:
 *
 *   1. `/` opens it from anywhere outside a text field. The composer handles its
 *      own `/`, so this is the global fallback for "I want a command".
 *   2. Focus always comes back to the composer. Opening parks focus there so the
 *      dialog's own focus restoration has somewhere sensible to return to, and
 *      closing focuses it directly - Escape must never drop focus on `<body>`.
 */
export function useCommandPalette(
  composerRef: React.RefObject<HTMLTextAreaElement | null>,
): CommandPaletteControl {
  const [open, setOpen] = React.useState(false);

  const focusComposer = React.useCallback(() => {
    composerRef.current?.focus();
  }, [composerRef]);

  const openPalette = React.useCallback(() => {
    focusComposer();
    setOpen(true);
  }, [focusComposer]);

  const onOpenChange = React.useCallback(
    (next: boolean) => {
      setOpen(next);
      if (!next) {
        window.requestAnimationFrame(() => focusComposer());
        window.setTimeout(() => focusComposer(), 180);
      }
    },
    [focusComposer],
  );

  React.useEffect(() => {
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target as HTMLElement | null;
      const tag = target?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || target?.isContentEditable) return;
      event.preventDefault();
      openPalette();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [openPalette]);

  return { open, openPalette, onOpenChange };
}
