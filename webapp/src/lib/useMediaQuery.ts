import * as React from "react";

/**
 * A CSS media query as React state.
 *
 * The sidebar is a column on a wide screen and a drawer on a narrow one, and those
 * are different DOM: a drawer needs a backdrop, Escape handling and focus moved
 * back to its toggle, none of which a `lg:hidden` class can do. So the layout
 * choice is made in JS, and a layout that is `false` when the query cannot be
 * evaluated at all (no `matchMedia`, e.g. a headless render) is the safe default:
 * a rail that squeezes the transcript is visible, an invisible drawer is not.
 */
export function useMediaQuery(query: string, fallback = false): boolean {
  const [matches, setMatches] = React.useState<boolean>(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return fallback;
    return window.matchMedia(query).matches;
  });

  React.useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const list = window.matchMedia(query);
    const update = (): void => setMatches(list.matches);
    update();
    // `addListener` is the Safari < 14 spelling; keeping both is cheaper than a
    // version check and avoids a silent no-op listener on an older phone.
    if (typeof list.addEventListener === "function") {
      list.addEventListener("change", update);
      return () => list.removeEventListener("change", update);
    }
    list.addListener(update);
    return () => list.removeListener(update);
  }, [query]);

  return matches;
}
