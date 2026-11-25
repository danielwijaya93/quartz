import { describe, expect, it } from "vitest";
import {
  MAX_TILES,
  bindTileSessionId,
  canAddTile,
  closeTile,
  createInitialTilingState,
  flipThreeOrientation,
  focusTileByIndex,
  openNewEmptySessionTile,
  openSessionInNewTile,
  openWorkerInNewTile,
  promoteTileToPrimary,
  readPersistedTilingState,
  resetSplitRatios,
  selectSessionInFocusedTile,
  setSplitRatio,
  setTileCount,
  tileBadgesBySessionId,
  toggleMaximizeTile,
  writePersistedTilingState,
} from "./tiling";

describe("tiling state engine", () => {
  it("starts with 1 focused session tile and default 50/50 ratios", () => {
    const state = createInitialTilingState();
    expect(state.tiles).toHaveLength(1);
    expect(state.tiles[0]?.kind).toBe("session");
    expect(state.focusedTileId).toBe(state.tiles[0]?.id);
    expect(state.maximizedTileId).toBeNull();
    expect(state.threeOrientation).toBe("top");
    expect(state.ratios).toEqual({ col: 0.5, row: 0.5 });
  });

  it("opens up to 4 tiles and caps at MAX_TILES without exceeding 4", () => {
    let state = createInitialTilingState();
    state = bindTileSessionId(state, state.tiles[0]!.id, "s1");
    state = openSessionInNewTile(state, "s2");
    state = openSessionInNewTile(state, "s3");
    state = openSessionInNewTile(state, "s4");

    expect(state.tiles).toHaveLength(MAX_TILES);
    expect(canAddTile(state)).toBe(false);
    expect(state.focusedTileId).toBe(state.tiles[3]!.id);

    // Unhappy path: opening a 5th distinct session replaces the focused tile instead of exceeding 4
    const next = openSessionInNewTile(state, "s5");
    expect(next.tiles).toHaveLength(4);
    expect(next.tiles[3]).toMatchObject({ kind: "session", sessionId: "s5" });
  });

  it("focuses an existing tile instead of duplicating an already-open session", () => {
    let state = createInitialTilingState();
    state = bindTileSessionId(state, state.tiles[0]!.id, "s1");
    state = openSessionInNewTile(state, "s2");
    expect(state.focusedTileId).toBe(state.tiles[1]!.id);

    // Selecting s1 in focused tile or via new-tile focuses tile 1 instead of duplicating
    const viaSelect = selectSessionInFocusedTile(state, "s1");
    expect(viaSelect.tiles).toHaveLength(2);
    expect(viaSelect.focusedTileId).toBe(state.tiles[0]!.id);

    const viaNew = openSessionInNewTile(state, "s1");
    expect(viaNew.tiles).toHaveLength(2);
    expect(viaNew.focusedTileId).toBe(state.tiles[0]!.id);
  });

  it("supports setTileCount 1->2->3->4 and shrinks while preserving the focused tile", () => {
    let state = createInitialTilingState();
    state = bindTileSessionId(state, state.tiles[0]!.id, "s1");
    state = setTileCount(state, 4, "/repo");
    expect(state.tiles).toHaveLength(4);

    // Focus tile 4 (index 3), then shrink to 2 -> tile 4 must survive!
    const tile4Id = state.tiles[3]!.id;
    state = focusTileByIndex(state, 3);
    expect(state.focusedTileId).toBe(tile4Id);

    const shrunk = setTileCount(state, 2);
    expect(shrunk.tiles).toHaveLength(2);
    expect(shrunk.tiles.some((t) => t.id === tile4Id)).toBe(true);
    expect(shrunk.focusedTileId).toBe(tile4Id);
  });

  it("reflows cleanly on close and refuses to close the last remaining tile", () => {
    let state = createInitialTilingState();
    const firstId = state.tiles[0]!.id;
    expect(closeTile(state, firstId)).toBe(state);

    state = openSessionInNewTile(state, "s2");
    state = openSessionInNewTile(state, "s3");
    const middleId = state.tiles[1]!.id;
    state = focusTileByIndex(state, 1);

    const afterClose = closeTile(state, middleId);
    expect(afterClose.tiles).toHaveLength(2);
    expect(afterClose.tiles.some((t) => t.id === middleId)).toBe(false);
    expect(afterClose.focusedTileId).toBe(afterClose.tiles[0]!.id);
  });

  it("handles maximize, restore, and closing a maximized tile safely", () => {
    let state = createInitialTilingState();
    state = bindTileSessionId(state, state.tiles[0]!.id, "s1");
    // Cannot maximize when only 1 tile exists
    expect(toggleMaximizeTile(state).maximizedTileId).toBeNull();

    state = openSessionInNewTile(state, "s2");
    const secondId = state.tiles[1]!.id;
    state = toggleMaximizeTile(state, secondId);
    expect(state.maximizedTileId).toBe(secondId);

    // Toggling again restores
    expect(toggleMaximizeTile(state, secondId).maximizedTileId).toBeNull();

    // Closing the maximized tile clears maximize cleanly
    const afterClose = closeTile(state, secondId);
    expect(afterClose.tiles).toHaveLength(1);
    expect(afterClose.maximizedTileId).toBeNull();
  });

  it("opens worker tail tiles, deduplicates by (sessionId, pid), and flips 3-tile orientation", () => {
    let state = createInitialTilingState();
    state = openWorkerInNewTile(state, {
      sessionId: "s1",
      pid: "4201",
      label: "worker-grid",
      command: "quartz -p worker",
    });
    expect(state.tiles).toHaveLength(2);
    expect(state.tiles[1]).toMatchObject({ kind: "worker", sessionId: "s1", pid: "4201" });

    // Re-opening same worker focuses existing tile instead of duplicating
    state = focusTileByIndex(state, 0);
    const deduped = openWorkerInNewTile(state, {
      sessionId: "s1",
      pid: "4201",
      label: "worker-grid",
      command: "quartz -p worker",
    });
    expect(deduped.tiles).toHaveLength(2);
    expect(deduped.focusedTileId).toBe(state.tiles[1]!.id);

    // 3-tile flip + promote to primary slot
    let s3 = openNewEmptySessionTile(deduped, "/repo");
    expect(s3.threeOrientation).toBe("top");
    s3 = flipThreeOrientation(s3);
    expect(s3.threeOrientation).toBe("bottom");
    const thirdId = s3.tiles[2]!.id;
    s3 = promoteTileToPrimary(s3, thirdId);
    expect(s3.tiles[0]!.id).toBe(thirdId);
    expect(s3.focusedTileId).toBe(thirdId);
  });

  it("clamps split ratios between 0.2 and 0.8 and resets to 0.5", () => {
    let state = createInitialTilingState();
    state = setSplitRatio(state, "col", 0.05);
    state = setSplitRatio(state, "row", 0.99);
    expect(state.ratios).toEqual({ col: 0.2, row: 0.8 });

    state = resetSplitRatios(state);
    expect(state.ratios).toEqual({ col: 0.5, row: 0.5 });
  });

  it("produces 1-based sidebar tile badges and round-trips safely through storage", () => {
    let state = createInitialTilingState();
    state = bindTileSessionId(state, state.tiles[0]!.id, "s1");
    state = openSessionInNewTile(state, "s2");
    state = flipThreeOrientation(state);
    state = setSplitRatio(state, "col", 0.62);

    const badges = tileBadgesBySessionId(state);
    expect(badges.get("s1")).toEqual({ number: 1, focused: false });
    expect(badges.get("s2")).toEqual({ number: 2, focused: true });

    const mem = new Map<string, string>();
    const fakeStorage = {
      getItem: (k: string) => mem.get(k) ?? null,
      setItem: (k: string, v: string) => {
        mem.set(k, v);
      },
    };
    writePersistedTilingState(state, fakeStorage);
    const restored = readPersistedTilingState(fakeStorage);
    expect(restored.tiles).toHaveLength(2);
    expect(restored.tiles[0]).toMatchObject({ kind: "session", sessionId: "s1" });
    expect(restored.tiles[1]).toMatchObject({ kind: "session", sessionId: "s2" });
    expect(restored.threeOrientation).toBe("bottom");
    expect(restored.ratios.col).toBeCloseTo(0.62);

    // Corrupt storage falls back safely to 1 initial tile
    fakeStorage.setItem("quartz:tiling:v1", "{not-json");
    const fallback = readPersistedTilingState(fakeStorage);
    expect(fallback.tiles).toHaveLength(1);
  });
});
