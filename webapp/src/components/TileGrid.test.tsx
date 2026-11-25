import * as React from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { TileGrid } from "./TileGrid";
import type { TileDescriptor } from "@/lib/tiling";

const TILES: readonly TileDescriptor[] = [
  { id: "tile-1", kind: "session", sessionId: "s1" },
  { id: "tile-2", kind: "session", sessionId: "s2" },
  { id: "tile-3", kind: "session", sessionId: "s3" },
  { id: "tile-4", kind: "session", sessionId: "s4" },
];

describe("TileGrid", () => {
  it("renders 2 vertical, 3 hybrid (top/bottom flip), and 4 equal grid layouts", () => {
    const { rerender, container } = render(
      <TileGrid
        tiles={TILES.slice(0, 2)}
        focusedTileId="tile-1"
        maximizedTileId={null}
        threeOrientation="top"
        ratios={{ col: 0.5, row: 0.5 }}
        narrow={false}
        onRatioChange={vi.fn()}
        onResetRatios={vi.fn()}
        renderTile={(tile, idx) => <div data-testid={`pane-${tile.id}`}>Pane {idx + 1}</div>}
      />,
    );
    expect(container.querySelector("[data-tile-layout='2']")).toBeInTheDocument();
    expect(screen.getByTestId("pane-tile-1")).toBeVisible();
    expect(screen.getByTestId("pane-tile-2")).toBeVisible();

    rerender(
      <TileGrid
        tiles={TILES.slice(0, 3)}
        focusedTileId="tile-1"
        maximizedTileId={null}
        threeOrientation="bottom"
        ratios={{ col: 0.5, row: 0.5 }}
        narrow={false}
        onRatioChange={vi.fn()}
        onResetRatios={vi.fn()}
        renderTile={(tile, idx) => <div data-testid={`pane-${tile.id}`}>Pane {idx + 1}</div>}
      />,
    );
    const layout3 = container.querySelector("[data-tile-layout='3']");
    expect(layout3).toHaveAttribute("data-three-orientation", "bottom");

    rerender(
      <TileGrid
        tiles={TILES}
        focusedTileId="tile-1"
        maximizedTileId={null}
        threeOrientation="top"
        ratios={{ col: 0.5, row: 0.5 }}
        narrow={false}
        onRatioChange={vi.fn()}
        onResetRatios={vi.fn()}
        renderTile={(tile, idx) => <div data-testid={`pane-${tile.id}`}>Pane {idx + 1}</div>}
      />,
    );
    expect(container.querySelector("[data-tile-layout='4']")).toBeInTheDocument();
    expect(screen.getByTestId("pane-tile-4")).toBeVisible();
  });

  it("keeps hidden tiles mounted when maximized or on narrow viewports", () => {
    render(
      <TileGrid
        tiles={TILES.slice(0, 2)}
        focusedTileId="tile-2"
        maximizedTileId="tile-2"
        threeOrientation="top"
        ratios={{ col: 0.5, row: 0.5 }}
        narrow={false}
        onRatioChange={vi.fn()}
        onResetRatios={vi.fn()}
        renderTile={(tile, idx) => <div data-testid={`pane-${tile.id}`}>Pane {idx + 1}</div>}
      />,
    );
    // Both remain in DOM (connections preserved), only maximized tile is shown
    expect(screen.getByTestId("pane-tile-1")).toBeInTheDocument();
    expect(screen.getByTestId("pane-tile-1").closest("[data-tile-hidden='true']")).not.toBeNull();
    expect(screen.getByTestId("pane-tile-2").closest("[data-tile-hidden='false']")).not.toBeNull();
  });

  it("resets split ratios on divider double-click", () => {
    const onReset = vi.fn();
    render(
      <TileGrid
        tiles={TILES.slice(0, 2)}
        focusedTileId="tile-1"
        maximizedTileId={null}
        threeOrientation="top"
        ratios={{ col: 0.65, row: 0.5 }}
        narrow={false}
        onRatioChange={vi.fn()}
        onResetRatios={onReset}
        renderTile={(tile) => <div>{tile.id}</div>}
      />,
    );
    const divider = screen.getByRole("separator", { name: "Resize vertical split" });
    fireEvent.doubleClick(divider);
    expect(onReset).toHaveBeenCalledTimes(1);
  });

  it("never unmounts surviving tiles across 1 <-> 2 <-> 3 -> 1, maximize, and narrow transitions", () => {
    const mounts = new Map<string, number>();
    const unmounts = new Map<string, number>();

    function MountTracker({ id }: { id: string }): React.ReactElement {
      React.useEffect(() => {
        mounts.set(id, (mounts.get(id) ?? 0) + 1);
        return () => {
          unmounts.set(id, (unmounts.get(id) ?? 0) + 1);
        };
      }, [id]);
      return <div data-testid={`tracked-${id}`}>{id}</div>;
    }

    const renderTracked = (tile: TileDescriptor) => <MountTracker id={tile.id} />;

    const { rerender } = render(
      <TileGrid
        tiles={TILES.slice(0, 1)}
        focusedTileId="tile-1"
        maximizedTileId={null}
        threeOrientation="top"
        ratios={{ col: 0.5, row: 0.5 }}
        narrow={false}
        onRatioChange={vi.fn()}
        onResetRatios={vi.fn()}
        renderTile={renderTracked}
      />,
    );
    expect(mounts.get("tile-1")).toBe(1);
    expect(unmounts.get("tile-1") ?? 0).toBe(0);

    // 1 -> 2 -> 3 tiles
    rerender(
      <TileGrid
        tiles={TILES.slice(0, 2)}
        focusedTileId="tile-2"
        maximizedTileId={null}
        threeOrientation="top"
        ratios={{ col: 0.5, row: 0.5 }}
        narrow={false}
        onRatioChange={vi.fn()}
        onResetRatios={vi.fn()}
        renderTile={renderTracked}
      />,
    );
    rerender(
      <TileGrid
        tiles={TILES.slice(0, 3)}
        focusedTileId="tile-3"
        maximizedTileId={null}
        threeOrientation="top"
        ratios={{ col: 0.5, row: 0.5 }}
        narrow={false}
        onRatioChange={vi.fn()}
        onResetRatios={vi.fn()}
        renderTile={renderTracked}
      />,
    );

    // Maximize tile 2, restore, switch to narrow, restore
    rerender(
      <TileGrid
        tiles={TILES.slice(0, 3)}
        focusedTileId="tile-2"
        maximizedTileId="tile-2"
        threeOrientation="top"
        ratios={{ col: 0.5, row: 0.5 }}
        narrow={false}
        onRatioChange={vi.fn()}
        onResetRatios={vi.fn()}
        renderTile={renderTracked}
      />,
    );
    rerender(
      <TileGrid
        tiles={TILES.slice(0, 3)}
        focusedTileId="tile-2"
        maximizedTileId={null}
        threeOrientation="top"
        ratios={{ col: 0.5, row: 0.5 }}
        narrow={true}
        onRatioChange={vi.fn()}
        onResetRatios={vi.fn()}
        renderTile={renderTracked}
      />,
    );
    rerender(
      <TileGrid
        tiles={TILES.slice(0, 3)}
        focusedTileId="tile-2"
        maximizedTileId={null}
        threeOrientation="top"
        ratios={{ col: 0.5, row: 0.5 }}
        narrow={false}
        onRatioChange={vi.fn()}
        onResetRatios={vi.fn()}
        renderTile={renderTracked}
      />,
    );

    // Close back down 3 -> 2 -> 1 tile (keeping tile-1)
    rerender(
      <TileGrid
        tiles={TILES.slice(0, 2)}
        focusedTileId="tile-1"
        maximizedTileId={null}
        threeOrientation="top"
        ratios={{ col: 0.5, row: 0.5 }}
        narrow={false}
        onRatioChange={vi.fn()}
        onResetRatios={vi.fn()}
        renderTile={renderTracked}
      />,
    );
    rerender(
      <TileGrid
        tiles={TILES.slice(0, 1)}
        focusedTileId="tile-1"
        maximizedTileId={null}
        threeOrientation="top"
        ratios={{ col: 0.5, row: 0.5 }}
        narrow={false}
        onRatioChange={vi.fn()}
        onResetRatios={vi.fn()}
        renderTile={renderTracked}
      />,
    );

    // Surviving tile-1 must never have unmounted or remounted.
    expect(mounts.get("tile-1")).toBe(1);
    expect(unmounts.get("tile-1") ?? 0).toBe(0);
    // Closed tiles mounted once and unmounted once (only when removed).
    expect(mounts.get("tile-2")).toBe(1);
    expect(unmounts.get("tile-2")).toBe(1);
    expect(mounts.get("tile-3")).toBe(1);
    expect(unmounts.get("tile-3")).toBe(1);
  });
});
