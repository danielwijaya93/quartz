import * as React from "react";
import { cn } from "@heroui/react";
import type { SplitRatios, ThreeTileOrientation, TileDescriptor } from "@/lib/tiling";

export interface TileGridProps {
  tiles: readonly TileDescriptor[];
  focusedTileId: string;
  maximizedTileId: string | null;
  threeOrientation: ThreeTileOrientation;
  ratios: SplitRatios;
  narrow: boolean;
  onRatioChange: (axis: keyof SplitRatios, value: number) => void;
  onResetRatios: () => void;
  renderTile: (tile: TileDescriptor, index: number) => React.ReactNode;
}

/**
 * Dynamic 1-4 pane tiling grid.
 *
 * All mounted tiles stay in the DOM even when one tile is maximized or the
 * viewport shrinks below 1024px, so WebSocket streams, scroll positions, and
 * composer drafts survive layout transitions intact.
 */
export function TileGrid({
  tiles,
  focusedTileId,
  maximizedTileId,
  threeOrientation,
  ratios,
  narrow,
  onRatioChange,
  onResetRatios,
  renderTile,
}: TileGridProps): React.ReactElement {
  const containerRef = React.useRef<HTMLDivElement | null>(null);
  const [dragging, setDragging] = React.useState<keyof SplitRatios | null>(null);

  const count = tiles.length;
  const singleVisibleId =
    count === 1
      ? tiles[0]!.id
      : maximizedTileId && tiles.some((t) => t.id === maximizedTileId)
        ? maximizedTileId
        : narrow
          ? focusedTileId
          : null;

  const startDrag = React.useCallback(
    (axis: keyof SplitRatios) => (event: React.PointerEvent<HTMLDivElement>) => {
      event.preventDefault();
      setDragging(axis);
      const container = containerRef.current;
      if (!container) return;

      const onMove = (moveEvent: PointerEvent): void => {
        const rect = container.getBoundingClientRect();
        if (axis === "col" && rect.width > 0) {
          onRatioChange("col", (moveEvent.clientX - rect.left) / rect.width);
        } else if (axis === "row" && rect.height > 0) {
          onRatioChange("row", (moveEvent.clientY - rect.top) / rect.height);
        }
      };

      const onUp = (): void => {
        setDragging(null);
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
      };

      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    },
    [onRatioChange],
  );

  // Single visible mode (1 tile, maximized tile, or mobile fallback)
  if (singleVisibleId !== null) {
    return (
      <div
        ref={containerRef}
        data-tile-layout="1"
        className="relative flex min-h-0 flex-1 flex-col overflow-hidden"
      >
        {tiles.map((tile, index) => {
          const hidden = tile.id !== singleVisibleId;
          return (
            <div
              key={tile.id}
              data-tile-id={tile.id}
              data-tile-hidden={hidden ? "true" : "false"}
              className={cn(
                "min-h-0 min-w-0 flex-1 flex-col",
                hidden ? "hidden" : "flex h-full w-full",
              )}
            >
              {renderTile(tile, index)}
            </div>
          );
        })}
      </div>
    );
  }

  const colPct = `${(ratios.col * 100).toFixed(2)}%`;
  const rowPct = `${(ratios.row * 100).toFixed(2)}%`;

  const gridStyle: React.CSSProperties =
    count === 2
      ? {
          gridTemplateColumns: `${colPct} 1fr`,
          gridTemplateRows: "1fr",
        }
      : {
          gridTemplateColumns: `${colPct} 1fr`,
          gridTemplateRows: `${rowPct} 1fr`,
        };

  const slotPlacement = (index: number): React.CSSProperties => {
    if (count === 2) {
      return { gridColumn: index === 0 ? 1 : 2, gridRow: 1 };
    }
    if (count === 3) {
      if (threeOrientation === "top") {
        if (index === 0) return { gridColumn: "1 / span 2", gridRow: 1 };
        return { gridColumn: index === 1 ? 1 : 2, gridRow: 2 };
      }
      // Flipped: 2 vertical panes on top, 1 long horizontal pane on bottom
      if (index === 0) return { gridColumn: "1 / span 2", gridRow: 2 };
      return { gridColumn: index === 1 ? 1 : 2, gridRow: 1 };
    }
    // 4-tile equal 2x2 grid
    return {
      gridColumn: index % 2 === 0 ? 1 : 2,
      gridRow: index < 2 ? 1 : 2,
    };
  };

  const showColDivider = count >= 2;
  const showRowDivider = count >= 3;

  // For 3-tile mode, the vertical divider only spans the split half (bottom or top).
  const colDividerStyle: React.CSSProperties =
    count === 3
      ? {
          left: `calc(${colPct} - 4px)`,
          top: threeOrientation === "top" ? rowPct : 0,
          height: threeOrientation === "top" ? `calc(100% - ${rowPct})` : rowPct,
        }
      : {
          left: `calc(${colPct} - 4px)`,
          top: 0,
          height: "100%",
        };

  return (
    <div
      ref={containerRef}
      data-tile-layout={String(count)}
      data-three-orientation={count === 3 ? threeOrientation : undefined}
      style={gridStyle}
      className={cn(
        "relative grid min-h-0 flex-1 gap-2 overflow-hidden bg-surface-secondary/20 p-2",
        dragging && "select-none",
      )}
    >
      {tiles.map((tile, index) => (
        <div
          key={tile.id}
          data-tile-id={tile.id}
          data-tile-hidden="false"
          style={slotPlacement(index)}
          className="flex min-h-0 min-w-0 flex-col overflow-hidden"
        >
          {renderTile(tile, index)}
        </div>
      ))}

      {showColDivider ? (
        <div
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize vertical split"
          title="Drag to resize · Double-click to reset"
          onPointerDown={startDrag("col")}
          onDoubleClick={onResetRatios}
          style={colDividerStyle}
          className={cn(
            "absolute z-10 w-2 cursor-col-resize rounded-full transition-colors",
            dragging === "col" ? "bg-sky-500/40" : "hover:bg-sky-500/25",
          )}
        />
      ) : null}

      {showRowDivider ? (
        <div
          role="separator"
          aria-orientation="horizontal"
          aria-label="Resize horizontal split"
          title="Drag to resize · Double-click to reset"
          onPointerDown={startDrag("row")}
          onDoubleClick={onResetRatios}
          style={{ top: `calc(${rowPct} - 4px)`, left: 0, width: "100%" }}
          className={cn(
            "absolute z-10 h-2 cursor-row-resize rounded-full transition-colors",
            dragging === "row" ? "bg-sky-500/40" : "hover:bg-sky-500/25",
          )}
        />
      ) : null}
    </div>
  );
}
