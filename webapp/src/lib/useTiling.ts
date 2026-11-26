import * as React from "react";
import {
  bindTileSessionId,
  canAddTile,
  closeTile,
  flipThreeOrientation,
  focusTile,
  focusTileByIndex,
  openNewEmptySessionTile,
  openSessionInNewTile,
  promoteTileToPrimary,
  readPersistedTilingState,
  resetSplitRatios,
  selectSessionInFocusedTile,
  setSplitRatio,
  setTileCount,
  tileBadgesBySessionId,
  toggleMaximizeTile,
  writePersistedTilingState,
  type SplitRatios,
  type TileBadge,
  type TileCount,
  type TilingState,
} from "./tiling";

const RATIO_PERSIST_DEBOUNCE_MS = 200;

export interface UseTilingApi {
  state: TilingState;
  canAdd: boolean;
  tileBadges: ReadonlyMap<string, TileBadge>;
  selectSession: (sessionId: string) => void;
  openSessionInNewTile: (sessionId: string) => void;
  openNewTile: (initialCwd?: string) => void;
  setTileCount: (count: TileCount, defaultCwd?: string) => void;
  closeTile: (tileId: string) => void;
  focusTile: (tileId: string) => void;
  focusTileByIndex: (index: number) => void;
  promoteTile: (tileId: string) => void;
  toggleMaximize: (tileId?: string) => void;
  flipThreeOrientation: () => void;
  setRatio: (axis: keyof SplitRatios, value: number) => void;
  resetRatios: () => void;
  bindSessionId: (tileId: string, sessionId: string) => void;
}

export function useTiling({
  hotkeysEnabled = true,
  defaultCwd,
  onKeyboardFocusTile,
}: {
  hotkeysEnabled?: boolean;
  defaultCwd?: string;
  /** Fired only when a tile is focused via Cmd/Ctrl+1..4 (so mouse clicks don't steal focus). */
  onKeyboardFocusTile?: (tileId: string) => void;
} = {}): UseTilingApi {
  const [state, setState] = React.useState<TilingState>(() => readPersistedTilingState());
  const defaultCwdRef = React.useRef(defaultCwd);
  defaultCwdRef.current = defaultCwd;
  const onKeyboardFocusRef = React.useRef(onKeyboardFocusTile);
  onKeyboardFocusRef.current = onKeyboardFocusTile;

  // Persist structural changes immediately, but debounce ratio-only updates so
  // dragging a split divider doesn't serialize to localStorage on every frame.
  const prevPersistedRef = React.useRef<TilingState | null>(null);
  const latestStateRef = React.useRef<TilingState>(state);
  latestStateRef.current = state;
  const persistTimerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  React.useEffect(() => {
    const prev = prevPersistedRef.current;
    const ratiosOnlyChange =
      prev !== null &&
      prev.tiles === state.tiles &&
      prev.focusedTileId === state.focusedTileId &&
      prev.threeOrientation === state.threeOrientation &&
      prev.ratios !== state.ratios;

    if (persistTimerRef.current !== null) {
      clearTimeout(persistTimerRef.current);
      persistTimerRef.current = null;
    }

    if (ratiosOnlyChange) {
      persistTimerRef.current = setTimeout(() => {
        persistTimerRef.current = null;
        prevPersistedRef.current = latestStateRef.current;
        writePersistedTilingState(latestStateRef.current);
      }, RATIO_PERSIST_DEBOUNCE_MS);
      return;
    }

    prevPersistedRef.current = state;
    writePersistedTilingState(state);
  }, [state]);

  React.useEffect(
    () => () => {
      if (persistTimerRef.current !== null) {
        clearTimeout(persistTimerRef.current);
        persistTimerRef.current = null;
        writePersistedTilingState(latestStateRef.current);
      }
    },
    [],
  );

  const selectSession = React.useCallback((sessionId: string) => {
    setState((prev) => selectSessionInFocusedTile(prev, sessionId));
  }, []);

  const openSessionInNewTileCb = React.useCallback((sessionId: string) => {
    setState((prev) => openSessionInNewTile(prev, sessionId));
  }, []);

  const openNewTile = React.useCallback((initialCwd?: string) => {
    setState((prev) => openNewEmptySessionTile(prev, initialCwd ?? defaultCwdRef.current));
  }, []);

  const setTileCountCb = React.useCallback((count: TileCount, cwd?: string) => {
    setState((prev) => setTileCount(prev, count, cwd ?? defaultCwdRef.current));
  }, []);

  const closeTileCb = React.useCallback((tileId: string) => {
    setState((prev) => closeTile(prev, tileId));
  }, []);

  const focusTileCb = React.useCallback((tileId: string) => {
    setState((prev) => focusTile(prev, tileId));
  }, []);

  const focusTileByIndexCb = React.useCallback((index: number) => {
    setState((prev) => focusTileByIndex(prev, index));
  }, []);

  const promoteTile = React.useCallback((tileId: string) => {
    setState((prev) => promoteTileToPrimary(prev, tileId));
  }, []);

  const toggleMaximize = React.useCallback((tileId?: string) => {
    setState((prev) => toggleMaximizeTile(prev, tileId));
  }, []);

  const flipThreeOrientationCb = React.useCallback(() => {
    setState((prev) => flipThreeOrientation(prev));
  }, []);

  const setRatio = React.useCallback((axis: keyof SplitRatios, value: number) => {
    setState((prev) => setSplitRatio(prev, axis, value));
  }, []);

  const resetRatios = React.useCallback(() => {
    setState((prev) => resetSplitRatios(prev));
  }, []);

  const bindSessionId = React.useCallback((tileId: string, sessionId: string) => {
    setState((prev) => bindTileSessionId(prev, tileId, sessionId));
  }, []);

  // Keyboard shortcuts: Cmd/Ctrl+1..4 focus tiles, Cmd/Ctrl+Shift+M maximize/restore, Cmd/Ctrl+\ add tile.
  React.useEffect(() => {
    if (!hotkeysEnabled) return;
    const onKeyDown = (event: KeyboardEvent): void => {
      const mod = event.metaKey || event.ctrlKey;
      if (!mod || event.altKey) return;
      if (!event.shiftKey && event.key >= "1" && event.key <= "4") {
        const index = Number(event.key) - 1;
        const target = latestStateRef.current.tiles[index];
        if (!target) return;
        event.preventDefault();
        setState((prev) => focusTileByIndex(prev, index));
        onKeyboardFocusRef.current?.(target.id);
        return;
      }
      if (event.shiftKey && event.key.toLowerCase() === "m") {
        setState((prev) => {
          if (prev.tiles.length <= 1) return prev;
          event.preventDefault();
          return toggleMaximizeTile(prev);
        });
        return;
      }
      if (!event.shiftKey && event.key === "\\") {
        setState((prev) => {
          if (!canAddTile(prev)) return prev;
          event.preventDefault();
          return openNewEmptySessionTile(prev, defaultCwdRef.current);
        });
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [hotkeysEnabled]);

  const tileBadges = React.useMemo(() => tileBadgesBySessionId(state), [state]);
  const canAdd = canAddTile(state);

  return React.useMemo(
    () => ({
      state,
      canAdd,
      tileBadges,
      selectSession,
      openSessionInNewTile: openSessionInNewTileCb,
      openNewTile,
      setTileCount: setTileCountCb,
      closeTile: closeTileCb,
      focusTile: focusTileCb,
      focusTileByIndex: focusTileByIndexCb,
      promoteTile,
      toggleMaximize,
      flipThreeOrientation: flipThreeOrientationCb,
      setRatio,
      resetRatios,
      bindSessionId,
    }),
    [
      state,
      canAdd,
      tileBadges,
      selectSession,
      openSessionInNewTileCb,
      openNewTile,
      setTileCountCb,
      closeTileCb,
      focusTileCb,
      focusTileByIndexCb,
      promoteTile,
      toggleMaximize,
      flipThreeOrientationCb,
      setRatio,
      resetRatios,
      bindSessionId,
    ],
  );
}
