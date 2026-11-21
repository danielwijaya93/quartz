/**
 * Theme contract.
 *
 * HeroUI v3 is CSS-driven: a `.dark` / `.light` class (plus the matching
 * `data-theme` attribute) on `<html>` swaps every design token. This module owns
 * the three-state intent behind that class - light, dark, or "follow the OS" -
 * and is the single place the rest of the app reads it from.
 *
 * `THEME_STORAGE_KEY` is duplicated by the inline bootstrap in `index.html`.
 * They are a pair: changing one without the other reintroduces a theme flash.
 */
import * as React from "react";
import { useTheme } from "@heroui/react";

export type ThemeMode = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";

/** Must match the key the inline bootstrap script in index.html reads. */
export const THEME_STORAGE_KEY = "heroui-theme";

export const THEME_MODES: readonly ThemeMode[] = ["light", "dark", "system"];

export function isThemeMode(value: unknown): value is ThemeMode {
  return value === "light" || value === "dark" || value === "system";
}

/**
 * Pure resolution of an intent to a concrete theme.
 *
 * "system" is the only mode that consults the OS, and it keeps doing so: the
 * calling hook re-renders when the media query changes, so an open tab follows a
 * daytime switch without a reload.
 */
export function resolveThemeMode(mode: ThemeMode, prefersDark: boolean): ResolvedTheme {
  if (mode === "dark") return "dark";
  if (mode === "light") return "light";
  return prefersDark ? "dark" : "light";
}

export interface ThemeController {
  /** The user's stored intent. */
  mode: ThemeMode;
  /** The concrete theme currently applied to the document. */
  resolved: ResolvedTheme;
  setMode: (mode: ThemeMode) => void;
}

/**
 * Thin typed wrapper over HeroUI's `useTheme`.
 *
 * HeroUI already persists the intent, resolves "system" against a live
 * `matchMedia` subscription, and applies the class in a layout effect, so there
 * is nothing to re-implement here - only to narrow `string` down to `ThemeMode`.
 */
export function useThemeMode(): ThemeController {
  const { theme, resolvedTheme, setTheme } = useTheme("system");
  const mode: ThemeMode = isThemeMode(theme) ? theme : "system";
  const resolved: ResolvedTheme = resolvedTheme === "dark" ? "dark" : "light";
  return { mode, resolved, setMode: setTheme };
}
/* ------------------------------------------------------- OpenUI theme mirror */

/**
 * Attribute the OpenUI component library reads its palette from.
 *
 * It is a *different* attribute from HeroUI's `data-theme` by design: two
 * independent design systems, each with its own contract. This module is the one
 * place they are reconciled.
 */
export const OPENUI_THEME_ATTRIBUTE = "data-openui-theme";

/**
 * Copy the theme HeroUI actually applied onto the OpenUI attribute.
 *
 * Reading the DOM rather than React state is deliberate. HeroUI's `useTheme`
 * holds its state *per hook call*, so a second `useTheme()` anywhere in the tree
 * gets an independent copy that never sees the theme control's updates -- which
 * left OpenUI surfaces painted in the wrong palette after switching to dark. The
 * document element is the single source of truth both systems write to, so it is
 * the one thing worth mirroring.
 *
 * Returns the resolved theme it applied.
 */
export function applyOpenUITheme(root: HTMLElement): ResolvedTheme {
  const resolved: ResolvedTheme =
    root.getAttribute("data-theme") === "dark" || root.classList.contains("dark")
      ? "dark"
      : "light";
  root.setAttribute(OPENUI_THEME_ATTRIBUTE, resolved);
  root.style.colorScheme = resolved;
  return resolved;
}

/**
 * Mirror the theme on `root` now, and again whenever it changes.
 *
 * The observer watches only `class` and `data-theme`, which are the two things
 * HeroUI writes, so its own writes to `data-openui-theme` cannot retrigger it.
 * Returns the disconnect function.
 */
export function observeOpenUITheme(root: HTMLElement): () => void {
  applyOpenUITheme(root);
  if (typeof MutationObserver === "undefined") return () => {};
  const observer = new MutationObserver(() => {
    applyOpenUITheme(root);
  });
  observer.observe(root, { attributes: true, attributeFilter: ["class", "data-theme"] });
  return () => observer.disconnect();
}

/** Keep the OpenUI palette in step with the app's theme for this component's life. */
export function useOpenUIThemeMirror(): void {
  React.useEffect(() => observeOpenUITheme(document.documentElement), []);
}
