import { describe, expect, it } from "vitest";
import indexHtml from "../../index.html?raw";
import {
  applyOpenUITheme,
  observeOpenUITheme,
  OPENUI_THEME_ATTRIBUTE,
  resolveThemeMode,
  THEME_MODES,
  THEME_STORAGE_KEY,
  isThemeMode,
} from "./theme";

describe("resolveThemeMode", () => {
  it("honours an explicit light or dark choice regardless of the OS", () => {
    expect(resolveThemeMode("light", true)).toBe("light");
    expect(resolveThemeMode("dark", false)).toBe("dark");
  });

  it("follows the OS preference in system mode", () => {
    expect(resolveThemeMode("system", true)).toBe("dark");
    expect(resolveThemeMode("system", false)).toBe("light");
  });

  it("recognises exactly the three modes", () => {
    expect(THEME_MODES).toEqual(["light", "dark", "system"]);
    expect(isThemeMode("system")).toBe(true);
    expect(isThemeMode("sepia")).toBe(false);
    expect(isThemeMode(null)).toBe(false);
  });
});

/**
 * The no-flash guarantee lives in `index.html`: an inline script that picks the
 * theme before the first paint. These tests run that exact script, extracted from
 * the document, so a change to it cannot silently regress the flash or drift from
 * the storage key the React hook reads.
 */
function bootstrapScript(): string {
  const match = /<script>([\s\S]*?)<\/script>/.exec(indexHtml);
  if (!match?.[1]) throw new Error("index.html has no inline theme bootstrap script");
  return match[1];
}

interface BootstrapResult {
  theme: string | null;
  classes: string[];
}

function runBootstrap({
  stored,
  prefersDark,
  storageThrows = false,
}: {
  stored: string | null;
  prefersDark: boolean;
  storageThrows?: boolean;
}): BootstrapResult {
  const classes: string[] = [];
  let theme: string | null = null;
  const documentStub = {
    documentElement: {
      classList: { add: (value: string) => classes.push(value) },
      setAttribute: (name: string, value: string) => {
        if (name === "data-theme") theme = value;
      },
    },
  };
  const windowStub = {
    localStorage: {
      getItem: (): string | null => {
        if (storageThrows) throw new Error("blocked");
        return stored;
      },
    },
    matchMedia: () => ({ matches: prefersDark }),
  };

  new Function("window", "document", bootstrapScript())(windowStub, documentStub);
  return { theme, classes };
}

describe("theme bootstrap in index.html", () => {
  it("reads the same storage key the React hook writes", () => {
    expect(bootstrapScript()).toContain(THEME_STORAGE_KEY);
  });

  it("does not hardcode a theme on the html element", () => {
    expect(indexHtml).not.toMatch(/<html[^>]*class="(light|dark)"/);
  });

  it("applies a stored dark choice instead of the OS preference", () => {
    const result = runBootstrap({ stored: "dark", prefersDark: false });
    expect(result.theme).toBe("dark");
    expect(result.classes).toEqual(["dark"]);
  });

  it("applies a stored light choice even when the OS is dark", () => {
    const result = runBootstrap({ stored: "light", prefersDark: true });
    expect(result.theme).toBe("light");
    expect(result.classes).toEqual(["light"]);
  });

  it("follows the OS when the stored choice is system", () => {
    expect(runBootstrap({ stored: "system", prefersDark: true }).theme).toBe("dark");
    expect(runBootstrap({ stored: "system", prefersDark: false }).theme).toBe("light");
  });

  it("defaults to the OS preference on a first visit", () => {
    expect(runBootstrap({ stored: null, prefersDark: true }).theme).toBe("dark");
    expect(runBootstrap({ stored: null, prefersDark: false }).theme).toBe("light");
  });

  it("still paints a theme when storage is blocked", () => {
    expect(runBootstrap({ stored: null, prefersDark: true, storageThrows: true }).theme).toBe("dark");
  });
});

/**
 * OpenUI has its own theme attribute, and it must follow whatever HeroUI applied.
 * These tests drive the mirror the way the theme control does: by changing the
 * attributes on the document element.
 */
describe("OpenUI theme mirror", () => {
  function root(): HTMLElement {
    const element = document.createElement("div");
    element.className = "light";
    element.setAttribute("data-theme", "light");
    return element;
  }

  it("reads the theme HeroUI applied and copies it", () => {
    const element = root();
    expect(applyOpenUITheme(element)).toBe("light");
    expect(element.getAttribute(OPENUI_THEME_ATTRIBUTE)).toBe("light");

    element.classList.remove("light");
    element.classList.add("dark");
    element.setAttribute("data-theme", "dark");
    expect(applyOpenUITheme(element)).toBe("dark");
    expect(element.getAttribute(OPENUI_THEME_ATTRIBUTE)).toBe("dark");
    expect(element.style.colorScheme).toBe("dark");
  });

  it("follows a change made after it started observing", async () => {
    const element = root();
    const disconnect = observeOpenUITheme(element);
    expect(element.getAttribute(OPENUI_THEME_ATTRIBUTE)).toBe("light");

    element.classList.replace("light", "dark");
    element.setAttribute("data-theme", "dark");

    // MutationObserver callbacks are delivered on a microtask.
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(element.getAttribute(OPENUI_THEME_ATTRIBUTE)).toBe("dark");
    disconnect();
  });

  it("stops following once disconnected", async () => {
    const element = root();
    const disconnect = observeOpenUITheme(element);
    disconnect();

    element.classList.replace("light", "dark");
    element.setAttribute("data-theme", "dark");
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(element.getAttribute(OPENUI_THEME_ATTRIBUTE)).toBe("light");
  });
});
