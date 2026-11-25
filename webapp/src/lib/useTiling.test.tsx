import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { TILING_STORAGE_KEY } from "./tiling";
import { useTiling } from "./useTiling";

describe("useTiling persistence & keyboard focus", () => {
  afterEach(() => {
    vi.useRealTimers();
    window.localStorage.removeItem(TILING_STORAGE_KEY);
  });

  it("debounces localStorage writes during rapid split-ratio drags", () => {
    vi.useFakeTimers();
    const setItemSpy = vi.spyOn(Storage.prototype, "setItem");

    const { result } = renderHook(() => useTiling());
    act(() => {
      result.current.openNewTile("/repo");
    });
    setItemSpy.mockClear();

    // Simulate 20 rapid pointermove ratio updates.
    act(() => {
      for (let i = 0; i < 20; i += 1) {
        result.current.setRatio("col", 0.3 + i * 0.01);
      }
    });

    // Must not synchronously write localStorage on every frame.
    expect(setItemSpy).not.toHaveBeenCalled();

    // After debounce delay, writes the latest ratio once.
    act(() => {
      vi.advanceTimersByTime(300);
    });
    expect(setItemSpy).toHaveBeenCalledTimes(1);
    const saved = JSON.parse(window.localStorage.getItem(TILING_STORAGE_KEY) ?? "{}") as {
      ratios?: { col?: number };
    };
    expect(saved.ratios?.col).toBeCloseTo(0.49, 2);

    setItemSpy.mockRestore();
  });

  it("invokes onKeyboardFocusTile only when focusing via Cmd/Ctrl+1..4 shortcut", () => {
    const onKeyboardFocusTile = vi.fn();
    const { result } = renderHook(() => useTiling({ onKeyboardFocusTile }));

    act(() => {
      result.current.openNewTile("/repo");
    });
    const firstTileId = result.current.state.tiles[0]!.id;

    // Regular focusTile (mouse click) does NOT trigger onKeyboardFocusTile.
    act(() => {
      result.current.focusTile(firstTileId);
    });
    expect(onKeyboardFocusTile).not.toHaveBeenCalled();

    // Cmd+2 focuses second tile AND calls onKeyboardFocusTile.
    const secondTileId = result.current.state.tiles[1]!.id;
    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "2", metaKey: true }));
    });
    expect(result.current.state.focusedTileId).toBe(secondTileId);
    expect(onKeyboardFocusTile).toHaveBeenCalledWith(secondTileId);
  });
});
