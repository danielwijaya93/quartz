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

  it("offers all three modes as labelled options, not one cycling button", () => {
    installMatchMedia(true);
    render(<ThemeToggle />);
    // A single-select group: HeroUI renders it as a radiogroup, which is exactly
    // the "choose one of three" semantics this control has.
    expect(screen.getByRole("radiogroup", { name: "Colour theme" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Light theme" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Dark theme" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "System theme" })).toBeInTheDocument();
  });

  it("marks the active mode as checked", () => {
    installMatchMedia(true);
    render(<ThemeToggle />);
    expect(screen.getByRole("radio", { name: "System theme" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByRole("radio", { name: "Light theme" })).toHaveAttribute(
      "aria-checked",
      "false",
    );
  });

  it("switches the document theme and stores the choice", async () => {
    const user = userEvent.setup();
    installMatchMedia(true);
    render(<ThemeToggle />);

    await user.click(screen.getByRole("radio", { name: "Light theme" }));
    expect(document.documentElement.classList.contains("light")).toBe(true);
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    expect(window.localStorage.getItem("heroui-theme")).toBe("light");

    await user.click(screen.getByRole("radio", { name: "Dark theme" }));
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(window.localStorage.getItem("heroui-theme")).toBe("dark");
  });

  it("returns to system and resolves against the OS", async () => {
    const user = userEvent.setup();
    installMatchMedia(true);
    render(<ThemeToggle />);

    await user.click(screen.getByRole("radio", { name: "Light theme" }));
    await user.click(screen.getByRole("radio", { name: "System theme" }));
    act(() => {});
    expect(window.localStorage.getItem("heroui-theme")).toBe("system");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });
});
