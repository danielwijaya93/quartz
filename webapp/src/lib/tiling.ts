/**
 * Pure state engine for the dynamic 1-4 pane tiling window manager.
 *
 * Layouts:
 *   - 1 tile: full single-session focus (identical to classic Quartz)
 *   - 2 tiles: vertical split (left | right)
 *   - 3 tiles: 1 long horizontal primary pane + 2 vertical panes (flippable top/bottom)
 *   - 4 tiles: equal 2x2 grid
 */

export const MAX_TILES = 4;
export const MIN_SPLIT_RATIO = 0.2;
export const MAX_SPLIT_RATIO = 0.8;
export const DEFAULT_SPLIT_RATIO = 0.5;
export const TILING_STORAGE_KEY = "quartz:tiling:v1";

export type TileCount = 1 | 2 | 3 | 4;
export type ThreeTileOrientation = "top" | "bottom";

export interface SessionTileDescriptor {
  readonly id: string;
  readonly kind: "session";
  /** Bound server sessionId, or null until `session_state` arrives. */
  readonly sessionId: string | null;
  /** Optional directory for starting a brand-new session inside this tile. */
  readonly initialCwd?: string;
}

export interface WorkerTileDescriptor {
  readonly id: string;
  readonly kind: "worker";
  readonly sessionId: string;
  readonly pid: string;
  readonly label: string;
  readonly command: string;
}

export type TileDescriptor = SessionTileDescriptor | WorkerTileDescriptor;

export interface SplitRatios {
  readonly col: number;
  readonly row: number;
}

export interface TilingState {
  readonly tiles: readonly TileDescriptor[];
  readonly focusedTileId: string;
  readonly maximizedTileId: string | null;
  readonly threeOrientation: ThreeTileOrientation;
  readonly ratios: SplitRatios;
}

export interface TileBadge {
  readonly number: number;
  readonly focused: boolean;
}

export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

let nextTileSeq = 1;

function nextTileId(existing: readonly TileDescriptor[]): string {
  const used = new Set(existing.map((t) => t.id));
  while (used.has(`tile-${nextTileSeq}`)) {
    nextTileSeq += 1;
  }
  const id = `tile-${nextTileSeq}`;
  nextTileSeq += 1;
  return id;
}

function clampRatio(value: number): number {
  if (!Number.isFinite(value)) return DEFAULT_SPLIT_RATIO;
  return Math.min(MAX_SPLIT_RATIO, Math.max(MIN_SPLIT_RATIO, value));
}

export function createInitialTilingState(initialSessionId: string | null = null): TilingState {
  const first: SessionTileDescriptor = {
    id: "tile-1",
    kind: "session",
    sessionId: initialSessionId,
  };
  return {
    tiles: [first],
    focusedTileId: first.id,
    maximizedTileId: null,
    threeOrientation: "top",
    ratios: { col: DEFAULT_SPLIT_RATIO, row: DEFAULT_SPLIT_RATIO },
  };
}

export function canAddTile(state: TilingState): boolean {
  return state.tiles.length < MAX_TILES;
}

export function findTileBySessionId(
  state: TilingState,
  sessionId: string,
): SessionTileDescriptor | undefined {
  return state.tiles.find(
    (t): t is SessionTileDescriptor => t.kind === "session" && t.sessionId === sessionId,
  );
}

export function focusTile(state: TilingState, tileId: string): TilingState {
  if (!state.tiles.some((t) => t.id === tileId)) return state;
  const nextMax =
    state.maximizedTileId !== null && state.maximizedTileId !== tileId ? null : state.maximizedTileId;
  if (state.focusedTileId === tileId && nextMax === state.maximizedTileId) return state;
  return { ...state, focusedTileId: tileId, maximizedTileId: nextMax };
}

export function focusTileByIndex(state: TilingState, index: number): TilingState {
  const tile = state.tiles[index];
  if (!tile) return state;
  return focusTile(state, tile.id);
}

/**
 * Load a session into the currently focused tile.
 * If that session is already open in another tile, focus that tile instead of duplicating it.
 */
export function selectSessionInFocusedTile(state: TilingState, sessionId: string): TilingState {
  const existing = findTileBySessionId(state, sessionId);
  if (existing) {
    return focusTile(state, existing.id);
  }
  const tiles = state.tiles.map((tile): TileDescriptor => {
    if (tile.id !== state.focusedTileId) return tile;
    return { id: tile.id, kind: "session", sessionId };
  });
  return { ...state, tiles };
}

/**
 * Open a session in a brand-new tile (up to MAX_TILES).
 * Deduplicates already-open sessions; when already at 4 tiles, replaces the focused tile.
 */
export function openSessionInNewTile(state: TilingState, sessionId: string): TilingState {
  const existing = findTileBySessionId(state, sessionId);
  if (existing) {
    return focusTile(state, existing.id);
  }
  if (state.tiles.length >= MAX_TILES) {
    return selectSessionInFocusedTile(state, sessionId);
  }
  const created: SessionTileDescriptor = {
    id: nextTileId(state.tiles),
    kind: "session",
    sessionId,
  };
  return {
    ...state,
    tiles: [...state.tiles, created],
    focusedTileId: created.id,
    maximizedTileId: null,
  };
}

export function openNewEmptySessionTile(state: TilingState, initialCwd?: string): TilingState {
  if (state.tiles.length >= MAX_TILES) return state;
  const created: SessionTileDescriptor = {
    id: nextTileId(state.tiles),
    kind: "session",
    sessionId: null,
    ...(initialCwd ? { initialCwd } : {}),
  };
  return {
    ...state,
    tiles: [...state.tiles, created],
    focusedTileId: created.id,
    maximizedTileId: null,
  };
}

export function openWorkerInNewTile(
  state: TilingState,
  worker: Omit<WorkerTileDescriptor, "id" | "kind">,
): TilingState {
  const existing = state.tiles.find(
    (t): t is WorkerTileDescriptor =>
      t.kind === "worker" && t.sessionId === worker.sessionId && t.pid === worker.pid,
  );
  if (existing) {
    return focusTile(state, existing.id);
  }
  if (state.tiles.length >= MAX_TILES) {
    const tiles = state.tiles.map((tile): TileDescriptor =>
      tile.id === state.focusedTileId
        ? { id: tile.id, kind: "worker", ...worker }
        : tile,
    );
    return { ...state, tiles, maximizedTileId: null };
  }
  const created: WorkerTileDescriptor = {
    id: nextTileId(state.tiles),
    kind: "worker",
    ...worker,
  };
  return {
    ...state,
    tiles: [...state.tiles, created],
    focusedTileId: created.id,
    maximizedTileId: null,
  };
}

export function setTileCount(
  state: TilingState,
  targetCount: TileCount,
  defaultCwd?: string,
): TilingState {
  if (targetCount === state.tiles.length && state.maximizedTileId === null) {
    return state;
  }
  if (targetCount > state.tiles.length) {
    let next: TilingState = { ...state, maximizedTileId: null };
    let firstAddedId: string | null = null;
    while (next.tiles.length < targetCount) {
      next = openNewEmptySessionTile(next, defaultCwd);
      if (firstAddedId === null) firstAddedId = next.focusedTileId;
    }
    return firstAddedId ? { ...next, focusedTileId: firstAddedId } : next;
  }
  // Shrink while guaranteeing the focused tile always survives.
  const focused = state.tiles.find((t) => t.id === state.focusedTileId) ?? state.tiles[0];
  const kept = new Set<string>(focused ? [focused.id] : []);
  for (const tile of state.tiles) {
    if (kept.size >= targetCount) break;
    kept.add(tile.id);
  }
  const nextTiles = state.tiles.filter((t) => kept.has(t.id));
  const nextFocus = nextTiles.some((t) => t.id === state.focusedTileId)
    ? state.focusedTileId
    : (nextTiles[0]?.id ?? state.focusedTileId);
  return {
    ...state,
    tiles: nextTiles,
    focusedTileId: nextFocus,
    maximizedTileId: null,
  };
}

export function closeTile(state: TilingState, tileId: string): TilingState {
  if (state.tiles.length <= 1) return state;
  const idx = state.tiles.findIndex((t) => t.id === tileId);
  if (idx === -1) return state;
  const nextTiles = state.tiles.filter((t) => t.id !== tileId);
  const nextFocus =
    state.focusedTileId === tileId
      ? (nextTiles[Math.max(0, idx - 1)]?.id ?? nextTiles[0]!.id)
      : state.focusedTileId;
  const nextMax =
    nextTiles.length <= 1 || state.maximizedTileId === tileId ? null : state.maximizedTileId;
  return {
    ...state,
    tiles: nextTiles,
    focusedTileId: nextFocus,
    maximizedTileId: nextMax,
  };
}

export function swapTiles(state: TilingState, tileIdA: string, tileIdB: string): TilingState {
  if (tileIdA === tileIdB) return state;
  const idxA = state.tiles.findIndex((t) => t.id === tileIdA);
  const idxB = state.tiles.findIndex((t) => t.id === tileIdB);
  if (idxA === -1 || idxB === -1) return state;
  const nextTiles = [...state.tiles];
  const a = nextTiles[idxA]!;
  nextTiles[idxA] = nextTiles[idxB]!;
  nextTiles[idxB] = a;
  return { ...state, tiles: nextTiles };
}

export function promoteTileToPrimary(state: TilingState, tileId: string): TilingState {
  const first = state.tiles[0];
  if (!first) return state;
  const swapped = first.id === tileId ? state : swapTiles(state, first.id, tileId);
  return focusTile(swapped, tileId);
}

export function toggleMaximizeTile(state: TilingState, tileId?: string): TilingState {
  if (state.tiles.length <= 1) {
    return state.maximizedTileId === null ? state : { ...state, maximizedTileId: null };
  }
  const target = tileId ?? state.focusedTileId;
  if (!state.tiles.some((t) => t.id === target)) return state;
  const nextMax = state.maximizedTileId === target ? null : target;
  return { ...state, focusedTileId: target, maximizedTileId: nextMax };
}

export function flipThreeOrientation(state: TilingState): TilingState {
  return {
    ...state,
    threeOrientation: state.threeOrientation === "top" ? "bottom" : "top",
  };
}

export function setSplitRatio(
  state: TilingState,
  axis: keyof SplitRatios,
  value: number,
): TilingState {
  const clamped = clampRatio(value);
  if (state.ratios[axis] === clamped) return state;
  return { ...state, ratios: { ...state.ratios, [axis]: clamped } };
}

export function resetSplitRatios(state: TilingState): TilingState {
  return { ...state, ratios: { col: DEFAULT_SPLIT_RATIO, row: DEFAULT_SPLIT_RATIO } };
}

export function bindTileSessionId(
  state: TilingState,
  tileId: string,
  sessionId: string,
): TilingState {
  if (!sessionId) return state;
  let changed = false;
  const tiles = state.tiles.map((tile): TileDescriptor => {
    if (tile.id !== tileId || tile.kind !== "session") return tile;
    if (tile.sessionId === sessionId && tile.initialCwd === undefined) return tile;
    changed = true;
    return { id: tile.id, kind: "session", sessionId };
  });
  return changed ? { ...state, tiles } : state;
}

export function tileBadgesBySessionId(state: TilingState): ReadonlyMap<string, TileBadge> {
  const map = new Map<string, TileBadge>();
  state.tiles.forEach((tile, index) => {
    if (tile.kind === "session" && tile.sessionId) {
      map.set(tile.sessionId, {
        number: index + 1,
        focused: tile.id === state.focusedTileId,
      });
    }
  });
  return map;
}

function defaultStorage(): StorageLike | null {
  try {
    return typeof window !== "undefined" ? window.localStorage : null;
  } catch {
    return null;
  }
}

function sanitizeTile(raw: unknown): TileDescriptor | null {
  if (!raw || typeof raw !== "object") return null;
  const obj = raw as Record<string, unknown>;
  const id = typeof obj.id === "string" && obj.id.trim() ? obj.id.trim() : null;
  if (!id) return null;
  if (obj.kind === "worker") {
    if (
      typeof obj.sessionId === "string" &&
      typeof obj.pid === "string" &&
      typeof obj.label === "string" &&
      typeof obj.command === "string"
    ) {
      return {
        id,
        kind: "worker",
        sessionId: obj.sessionId,
        pid: obj.pid,
        label: obj.label,
        command: obj.command,
      };
    }
    return null;
  }
  if (obj.kind === "session") {
    const sessionId =
      typeof obj.sessionId === "string" && obj.sessionId.trim() ? obj.sessionId.trim() : null;
    const initialCwd =
      typeof obj.initialCwd === "string" && obj.initialCwd.trim() ? obj.initialCwd.trim() : undefined;
    return { id, kind: "session", sessionId, ...(initialCwd ? { initialCwd } : {}) };
  }
  return null;
}

export function readPersistedTilingState(storage: StorageLike | null = defaultStorage()): TilingState {
  if (!storage) return createInitialTilingState();
  try {
    const raw = storage.getItem(TILING_STORAGE_KEY);
    if (!raw) return createInitialTilingState();
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const rawTiles = Array.isArray(parsed.tiles) ? parsed.tiles : [];
    const seenIds = new Set<string>();
    const tiles: TileDescriptor[] = [];
    for (const entry of rawTiles) {
      if (tiles.length >= MAX_TILES) break;
      const clean = sanitizeTile(entry);
      if (!clean || seenIds.has(clean.id)) continue;
      seenIds.add(clean.id);
      tiles.push(clean);
    }
    if (tiles.length === 0) return createInitialTilingState();
    const focusedTileId =
      typeof parsed.focusedTileId === "string" && seenIds.has(parsed.focusedTileId)
        ? parsed.focusedTileId
        : tiles[0]!.id;
    const threeOrientation: ThreeTileOrientation =
      parsed.threeOrientation === "bottom" ? "bottom" : "top";
    const rawRatios =
      parsed.ratios && typeof parsed.ratios === "object"
        ? (parsed.ratios as Record<string, unknown>)
        : {};
    const col = clampRatio(typeof rawRatios.col === "number" ? rawRatios.col : DEFAULT_SPLIT_RATIO);
    const row = clampRatio(typeof rawRatios.row === "number" ? rawRatios.row : DEFAULT_SPLIT_RATIO);
    return {
      tiles,
      focusedTileId,
      maximizedTileId: null,
      threeOrientation,
      ratios: { col, row },
    };
  } catch {
    return createInitialTilingState();
  }
}

export function writePersistedTilingState(
  state: TilingState,
  storage: StorageLike | null = defaultStorage(),
): void {
  if (!storage) return;
  try {
    storage.setItem(
      TILING_STORAGE_KEY,
      JSON.stringify({
        tiles: state.tiles,
        focusedTileId: state.focusedTileId,
        threeOrientation: state.threeOrientation,
        ratios: state.ratios,
      }),
    );
  } catch {
    // Ignore quota / private-browsing storage errors.
  }
}
