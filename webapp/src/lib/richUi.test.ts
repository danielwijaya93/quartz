import { describe, expect, it } from "vitest";
import {
  DEFAULT_RICH_UI_MODE,
  RICH_UI_MODES,
  RICH_UI_STORAGE_KEY,
  isRichUiMode,
  readRichUiMode,
  wantsRichUi,
  writeRichUiMode,
} from "./richUi";

function store(initial: Record<string, string> = {}): Pick<Storage, "getItem" | "setItem"> & {
  value: Map<string, string>;
} {
  const value = new Map(Object.entries(initial));
  return {
    value,
    getItem: (key: string) => value.get(key) ?? null,
    setItem: (key: string, next: string) => {
      value.set(key, next);
    },
  };
}

describe("rich UI preference", () => {
  it("defaults to markdown so a first visit never pays the prompt", () => {
    expect(DEFAULT_RICH_UI_MODE).toBe("markdown");
    expect(readRichUiMode(store())).toBe("markdown");
  });

  it("round-trips a stored choice", () => {
    const storage = store();
    writeRichUiMode("rich", storage);
    expect(storage.value.get(RICH_UI_STORAGE_KEY)).toBe("rich");
    expect(readRichUiMode(storage)).toBe("rich");
  });

  it("falls back to markdown for a value it does not recognise", () => {
    // A newer build could have written a third mode; an older tab must not crash.
    expect(readRichUiMode(store({ [RICH_UI_STORAGE_KEY]: "dashboard" }))).toBe("markdown");
  });

  it("survives storage that throws, in both directions", () => {
    const blocked = {
      value: new Map<string, string>(),
      getItem: () => {
        throw new Error("blocked by policy");
      },
      setItem: () => {
        throw new Error("quota");
      },
    };
    expect(readRichUiMode(blocked)).toBe("markdown");
    expect(() => writeRichUiMode("rich", blocked)).not.toThrow();
  });

  it("treats a missing storage implementation as the default", () => {
    // `localStorage` is undefined during the build-time render in some tooling.
    expect(readRichUiMode(undefined as unknown as Pick<Storage, "getItem">)).toBe("markdown");
  });

  it("recognises exactly the two modes, and only 'rich' asks for OpenUI", () => {
    expect(RICH_UI_MODES).toEqual(["markdown", "rich"]);
    expect(isRichUiMode("rich")).toBe(true);
    expect(isRichUiMode("markdown")).toBe(true);
    expect(isRichUiMode("Rich")).toBe(false);
    expect(wantsRichUi("rich")).toBe(true);
    expect(wantsRichUi("markdown")).toBe(false);
  });
});
