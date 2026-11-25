import * as React from "react";
import { cn } from "@heroui/react";
import { ArrowUpDown, Plus } from "lucide-react";
import type { ThreeTileOrientation, TileCount, TileDescriptor } from "@/lib/tiling";

function LayoutGlyph({ count, orientation }: { count: TileCount; orientation: ThreeTileOrientation }): React.ReactElement {
  if (count === 1) {
    return (
      <span aria-hidden="true" className="grid size-3 grid-cols-1 gap-[1.5px]">
        <span className="rounded-[1px] bg-current opacity-85" />
      </span>
    );
  }
  if (count === 2) {
    return (
      <span aria-hidden="true" className="grid h-2.5 w-3.5 grid-cols-2 gap-[1.5px]">
        <span className="rounded-[1px] bg-current opacity-85" />
        <span className="rounded-[1px] bg-current opacity-85" />
      </span>
    );
  }
  if (count === 3) {
    return (
      <span aria-hidden="true" className="grid h-2.5 w-3.5 grid-cols-2 grid-rows-2 gap-[1.5px]">
        {orientation === "top" ? (
          <>
            <span className="col-span-2 rounded-[1px] bg-current opacity-85" />
            <span className="rounded-[1px] bg-current opacity-85" />
            <span className="rounded-[1px] bg-current opacity-85" />
          </>
        ) : (
          <>
            <span className="rounded-[1px] bg-current opacity-85" />
            <span className="rounded-[1px] bg-current opacity-85" />
            <span className="col-span-2 rounded-[1px] bg-current opacity-85" />
          </>
        )}
      </span>
    );
  }
  return (
    <span aria-hidden="true" className="grid h-2.5 w-3.5 grid-cols-2 grid-rows-2 gap-[1.5px]">
      <span className="rounded-[1px] bg-current opacity-85" />
      <span className="rounded-[1px] bg-current opacity-85" />
      <span className="rounded-[1px] bg-current opacity-85" />
      <span className="rounded-[1px] bg-current opacity-85" />
    </span>
  );
}

const COUNTS: readonly TileCount[] = [1, 2, 3, 4];
const COUNT_LABELS: Record<TileCount, string> = {
  1: "1 window (single focus)",
  2: "2 windows (vertical split)",
  3: "3 windows (1 wide + 2 vertical)",
  4: "4 windows (2x2 grid)",
};

export function TileLayoutSwitcher({
  tiles,
  focusedTileId,
  threeOrientation,
  canAdd,
  narrow,
  onSetCount,
  onAddTile,
  onFlipThree,
  onFocusTile,
}: {
  tiles: readonly TileDescriptor[];
  focusedTileId: string;
  threeOrientation: ThreeTileOrientation;
  canAdd: boolean;
  narrow: boolean;
  onSetCount: (count: TileCount) => void;
  onAddTile: () => void;
  onFlipThree: () => void;
  onFocusTile: (tileId: string) => void;
}): React.ReactElement {
  const count = tiles.length as TileCount;

  // On narrow screens with multiple tiles open, show a compact 1 | 2 | 3 | 4 switcher pill bar.
  if (narrow) {
    if (tiles.length <= 1) {
      return (
        <button
          type="button"
          onClick={onAddTile}
          aria-label="Open a second tile"
          title="Split into 2 tiles"
          className="inline-flex h-7 cursor-pointer items-center gap-1 rounded-md border border-border/80 bg-surface-secondary/40 px-2 text-xs font-medium text-muted transition-colors hover:bg-surface-secondary hover:text-foreground focus-visible:ring-2 focus-visible:ring-focus focus-visible:outline-none"
        >
          <Plus aria-hidden="true" className="size-3" />
          <span className="sr-only">Add tile</span>
        </button>
      );
    }
    return (
      <div role="group" aria-label="Active tiles" className="inline-flex items-center gap-0.5 rounded-lg border border-border bg-surface-secondary/60 p-0.5">
        {tiles.map((tile, idx) => {
          const active = tile.id === focusedTileId;
          return (
            <button
              key={tile.id}
              type="button"
              onClick={() => onFocusTile(tile.id)}
              aria-pressed={active}
              aria-label={`Switch to tile ${idx + 1}`}
              className={cn(
                "inline-flex h-6 min-w-6 cursor-pointer items-center justify-center rounded-md px-1.5 text-[11px] font-semibold tabular-nums transition-colors focus-visible:ring-1 focus-visible:ring-focus focus-visible:outline-none",
                active
                  ? "bg-surface text-foreground shadow-xs"
                  : "text-muted hover:text-foreground",
              )}
            >
              {idx + 1}
            </button>
          );
        })}
        {canAdd ? (
          <button
            type="button"
            onClick={onAddTile}
            aria-label="Add tile"
            title="Add tile (max 4)"
            className="inline-flex h-6 w-6 cursor-pointer items-center justify-center rounded-md text-muted hover:text-foreground"
          >
            <Plus aria-hidden="true" className="size-3" />
          </button>
        ) : null}
      </div>
    );
  }

  return (
    <div className="flex items-center gap-1.5">
      <div
        role="group"
        aria-label="Tile layout"
        className="inline-flex items-center gap-0.5 rounded-lg border border-border bg-surface-secondary/50 p-0.5"
      >
        {COUNTS.map((n) => {
          const active = count === n;
          return (
            <button
              key={n}
              type="button"
              onClick={() => onSetCount(n)}
              aria-pressed={active}
              aria-label={COUNT_LABELS[n]}
              title={COUNT_LABELS[n]}
              className={cn(
                "inline-flex h-6 cursor-pointer items-center gap-1 rounded-md px-1.5 text-[11px] font-semibold tabular-nums transition-colors focus-visible:ring-1 focus-visible:ring-focus focus-visible:outline-none",
                active
                  ? "bg-surface text-foreground shadow-xs ring-1 ring-border"
                  : "text-muted hover:bg-surface/50 hover:text-foreground",
              )}
            >
              <LayoutGlyph count={n} orientation={threeOrientation} />
              <span>{n}</span>
            </button>
          );
        })}
      </div>

      {count === 3 ? (
        <button
          type="button"
          onClick={onFlipThree}
          aria-label="Flip 3-tile horizontal pane position"
          title={
            threeOrientation === "top"
              ? "Move wide pane to bottom"
              : "Move wide pane to top"
          }
          className="inline-flex h-7 cursor-pointer items-center gap-1 rounded-md border border-border/80 bg-surface-secondary/40 px-2 text-[11px] font-medium text-muted transition-colors hover:bg-surface-secondary hover:text-foreground focus-visible:ring-1 focus-visible:ring-focus focus-visible:outline-none"
        >
          <ArrowUpDown aria-hidden="true" className="size-3" />
          <span className="hidden lg:inline">Flip</span>
        </button>
      ) : null}

      <button
        type="button"
        disabled={!canAdd}
        onClick={onAddTile}
        aria-label={canAdd ? "Add tile" : "Maximum 4 tiles open"}
        title={canAdd ? "Add a new tile (⌘\)" : "Max 4 tiles open"}
        className={cn(
          "inline-flex h-7 items-center gap-1 rounded-md border border-border/80 bg-surface-secondary/40 px-2 text-[11px] font-medium transition-colors focus-visible:ring-1 focus-visible:ring-focus focus-visible:outline-none",
          canAdd
            ? "cursor-pointer text-muted hover:bg-surface-secondary hover:text-foreground"
            : "cursor-not-allowed text-meta opacity-50",
        )}
      >
        <Plus aria-hidden="true" className="size-3" />
        <span className="hidden xl:inline">Tile</span>
      </button>
    </div>
  );
}
