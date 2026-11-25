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
  type SplitRatios,
  type TileBadge,
  type TileCount,
  type TilingState,
  type WorkerTileDescriptor,
} from "./tiling";

export interface UseTilingApi {
  state: TilingState;
  canAdd: boolean;
  tileBadges: ReadonlyMap<string, TileBadge>;
  selectSession: (sessionId: string) => void;
  openSessionInNewTile: (sessionId: string) => void;
  openNewTile: (initialCwd?: string) => void;
  openWorkerInTile: (worker: Omit<WorkerTileDescriptor, "id" | "kind">) => void;
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
}: {
  hotkeysEnabled?: boolean;
  defaultCwd?: string;
} = {}): UseTilingApi {
  const [state, setState] = React.useState<TilingState>(() => readPersistedTilingState());
  const defaultCwdRef = React.useRef(defaultCwd);
  defaultCwdRef.current = defaultCwd;

  React.useEffect(() => {
    writePersistedTilingState(state);
  }, [state]);

  const selectSession = React.useCallback((sessionId: string) => {
    setState((prev) => selectSessionInFocusedTile(prev, sessionId));
  }, []);

  const openSessionInNewTileCb = React.useCallback((sessionId: string) => {
    setState((prev) => openSessionInNewTile(prev, sessionId));
  }, []);

  const openNewTile = React.useCallback((initialCwd?: string) => {
    setState((prev) => openNewEmptySessionTile(prev, initialCwd ?? defaultCwdRef.current));
  }, []);

  const openWorkerInTile = React.useCallback(
    (worker: Omit<WorkerTileDescriptor, "id" | "kind">) => {
      setState((prev) => openWorkerInNewTile(prev, worker));
    },
    [],
  );

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
        setState((prev) => {
          if (index >= prev.tiles.length) return prev;
          event.preventDefault();
          return focusTileByIndex(prev, index);
        });
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

  return {
    state,
    canAdd: canAddTile(state),
    tileBadges,
    selectSession,
    openSessionInNewTile: openSessionInNewTileCb,
    openNewTile,
    openWorkerInTile,
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
  };
}
