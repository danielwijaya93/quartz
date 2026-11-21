import { afterEach, describe, expect, it } from "vitest";
import { act, renderHook } from "@testing-library/react";
import {
  DEFAULT_RICH_UI_MODE,
  RICH_UI_STORAGE_KEY,
  parseRichUiMode,
  readRichUiMode,
  useRichUiMode,
  writeRichUiMode,
} from "./richUi";

afterEach(() => {
  window.localStorage.clear();
});

function memoryStorage(initial: Record<string, string> = {}) {
  const store = new Map(Object.entries(initial));
  return {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    raw: store,
  };
}

describe("parseRichUiMode", () => {
  it("defaults to markdown for anything unrecognised", () => {
    expect(DEFAULT_RICH_UI_MODE).toBe("markdown");
    expect(parseRichUiMode(null)).toBe("markdown");
    expect(parseRichUiMode("")).toBe("markdown");
    expect(parseRichUiMode("Dashboard")).toBe("markdown");
  });

  it("accepts both real states", () => {
    expect(parseRichUiMode("markdown")).toBe("markdown");
    expect(parseRichUiMode("dashboard")).toBe("dashboard");
  });
});

describe("storage", () => {
  it("round-trips the choice", () => {
    const storage = memoryStorage();
    writeRichUiMode("dashboard", storage);
    expect(readRichUiMode(storage)).toBe("dashboard");
    expect(storage.raw.get(RICH_UI_STORAGE_KEY)).toBe("dashboard");
  });

  it("falls back to markdown when nothing is stored", () => {
    expect(readRichUiMode(memoryStorage())).toBe("markdown");
  });

  it("treats blocked storage as no preference, not as a crash", () => {
    const throwing = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    };
    expect(readRichUiMode(throwing)).toBe("markdown");
    expect(() => writeRichUiMode("dashboard", throwing)).not.toThrow();
  });
});

describe("useRichUiMode", () => {
  it("restores the stored choice on load", () => {
    window.localStorage.setItem(RICH_UI_STORAGE_KEY, "dashboard");
    const { result } = renderHook(() => useRichUiMode());
    expect(result.current.mode).toBe("dashboard");
    expect(result.current.dashboard).toBe(true);
  });

  it("persists a change and exposes it as dashboard-on", () => {
    const { result } = renderHook(() => useRichUiMode());
    expect(result.current.dashboard).toBe(false);

    act(() => result.current.setMode("dashboard"));

    expect(result.current.mode).toBe("dashboard");
    expect(result.current.dashboard).toBe(true);
    expect(window.localStorage.getItem(RICH_UI_STORAGE_KEY)).toBe("dashboard");
  });

  it("survives a reload: a fresh hook reads what the last one wrote", () => {
    const first = renderHook(() => useRichUiMode());
    act(() => first.result.current.setMode("dashboard"));
    first.unmount();

    const second = renderHook(() => useRichUiMode());
    expect(second.result.current.mode).toBe("dashboard");
  });
});
