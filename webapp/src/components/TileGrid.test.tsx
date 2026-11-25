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
});
