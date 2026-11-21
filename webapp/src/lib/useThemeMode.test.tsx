import { afterEach, describe, expect, it } from "vitest";
import { act, render, screen } from "@testing-library/react";
import * as React from "react";
import { useThemeMode } from "./theme";

/**
 * Drives the system-preference path the way a real machine does: `matchMedia`
 * backed by a mutable value plus a listener set, so flipping the OS preference
 * fires `change` exactly like the browser does.
 */
function installMatchMedia(initialDark: boolean): { setDark: (dark: boolean) => void } {
  let dark = initialDark;
  const listeners = new Set<() => void>();

  const query = {
    get matches() {
      return dark;
    },
    media: "(prefers-color-scheme: dark)",
    onchange: null,
    addEventListener: (_: string, listener: () => void) => listeners.add(listener),
    removeEventListener: (_: string, listener: () => void) => listeners.delete(listener),
    addListener: (listener: () => void) => listeners.add(listener),
    removeListener: (listener: () => void) => listeners.delete(listener),
    dispatchEvent: () => true,
  };

  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: () => query,
  });

  return {
    setDark: (next: boolean) => {
      dark = next;
      for (const listener of listeners) listener();
    },
  };
}

function Probe(): React.ReactElement {
  const { mode, resolved, setMode } = useThemeMode();
  return (
    <div>
      <span data-testid="mode">{mode}</span>
      <span data-testid="resolved">{resolved}</span>
      <button type="button" onClick={() => setMode("dark")}>
        dark
      </button>
      <button type="button" onClick={() => setMode("light")}>
        light
      </button>
      <button type="button" onClick={() => setMode("system")}>
        system
      </button>
    </div>
  );
}

describe("useThemeMode", () => {
  afterEach(() => {
    window.localStorage.clear();
    document.documentElement.className = "";
    document.documentElement.removeAttribute("data-theme");
  });

  it("defaults to system and resolves from the OS", () => {
    installMatchMedia(true);
    render(<Probe />);
    expect(screen.getByTestId("mode")).toHaveTextContent("system");
    expect(screen.getByTestId("resolved")).toHaveTextContent("dark");
  });

  it("follows a live OS change while in system mode", () => {
    const media = installMatchMedia(false);
    render(<Probe />);
    expect(screen.getByTestId("resolved")).toHaveTextContent("light");

    act(() => media.setDark(true));
    expect(screen.getByTestId("resolved")).toHaveTextContent("dark");
  });

  it("applies both the class and the attribute to <html>", () => {
    installMatchMedia(false);
    render(<Probe />);
    becomeDark();
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });

  it("stops following the OS once a mode is chosen explicitly", () => {
    const media = installMatchMedia(false);
    render(<Probe />);
    act(() => screen.getByRole("button", { name: "dark" }).click());
    expect(screen.getByTestId("resolved")).toHaveTextContent("dark");
    act(() => media.setDark(false));
    expect(screen.getByTestId("resolved")).toHaveTextContent("dark");
  });

  it("persists the chosen intent under the key the bootstrap script reads", () => {
    installMatchMedia(true);
    render(<Probe />);
    act(() => screen.getByRole("button", { name: "light" }).click());
    expect(window.localStorage.getItem("heroui-theme")).toBe("light");
  });
});

function becomeDark(): void {
  const button = screen.getByRole("button", { name: "dark" });
  act(() => button.click());
}
