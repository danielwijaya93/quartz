import * as React from "react";

export interface SidebarDrawerProps {
  onClose: () => void;
  children: React.ReactNode;
}

/**
 * The phone presentation of the sidebar: a backdrop and a panel over the transcript.
 *
 * This is a component rather than a pair of CSS classes because a drawer has to do
 * three things a class cannot: answer Escape, move focus into itself when it opens,
 * and give focus back to whatever opened it when it closes. Without the last one,
 * closing the drawer leaves the keyboard on `<body>` and the next Tab starts from
 * the top of the page.
 *
 * No animation on purpose: the panel appears in one frame, so there is nothing to
 * slow down for `prefers-reduced-motion` and nothing to animate.
 */
export function SidebarDrawer({ onClose, children }: SidebarDrawerProps): React.ReactElement {
  const panelRef = React.useRef<HTMLDivElement | null>(null);
  const restoreRef = React.useRef<HTMLElement | null>(null);

  React.useEffect(() => {
    const active = document.activeElement;
    restoreRef.current = active instanceof HTMLElement ? active : null;
    panelRef.current?.focus();

    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      // The opener may itself be gone (a re-render replaced it); focusing a detached
      // node is a no-op, which is the right outcome for a button that no longer
      // exists.
      restoreRef.current?.focus();
    };
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-50 flex">
      <div className="fixed inset-0 bg-black/50" onClick={onClose} aria-hidden="true" />
      <div
        ref={panelRef}
        tabIndex={-1}
        className="relative z-10 h-full w-[min(85vw,18rem)] shadow-2xl focus:outline-none"
      >
        {children}
      </div>
    </div>
  );
}
