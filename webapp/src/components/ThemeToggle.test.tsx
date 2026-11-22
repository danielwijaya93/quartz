import { afterEach, describe, expect, it } from "vitest";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeToggle } from "./ThemeToggle";

function installMatchMedia(dark: boolean): void {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: () => ({
      matches: dark,
      media: "(prefers-color-scheme: dark)",
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => true,
    }),
  });
}

describe("ThemeToggle", () => {
  afterEach(() => {
    window.localStorage.clear();
    document.documentElement.className = "";
    document.documentElement.removeAttribute("data-theme");
  });

  it("renders a compact cycling button showing current theme", () => {
    installMatchMedia(true);
    render(<ThemeToggle />);
    const button = screen.getByRole("button", { name: /Theme: System/i });
    expect(button).toBeInTheDocument();
  });

  it("cycles through System -> Light -> Dark -> System", async () => {
    const user = userEvent.setup();
    installMatchMedia(true);
    render(<ThemeToggle />);

    // Initial state is system
    const button = screen.getByRole("button", { name: /Theme: System/i });
    expect(button).toBeInTheDocument();

    // Click 1: System -> Light
    await user.click(button);
    expect(document.documentElement.classList.contains("light")).toBe(true);
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    expect(window.localStorage.getItem("heroui-theme")).toBe("light");
    expect(screen.getByRole("button", { name: /Theme: Light/i })).toBeInTheDocument();

    // Click 2: Light -> Dark
    await user.click(screen.getByRole("button", { name: /Theme: Light/i }));
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(window.localStorage.getItem("heroui-theme")).toBe("dark");
    expect(screen.getByRole("button", { name: /Theme: Dark/i })).toBeInTheDocument();

    // Click 3: Dark -> System
    await user.click(screen.getByRole("button", { name: /Theme: Dark/i }));
    act(() => {});
    expect(window.localStorage.getItem("heroui-theme")).toBe("system");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(screen.getByRole("button", { name: /Theme: System/i })).toBeInTheDocument();
  });
});
