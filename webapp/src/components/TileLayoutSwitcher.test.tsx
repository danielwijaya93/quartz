import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MobileTileBar, TileLayoutSwitcher } from "./TileLayoutSwitcher";
import type { TileDescriptor } from "@/lib/tiling";

const TWO_TILES: readonly TileDescriptor[] = [
  { id: "tile-1", kind: "session", sessionId: "s1" },
  { id: "tile-2", kind: "session", sessionId: "s2" },
];

const FOUR_TILES: readonly TileDescriptor[] = [
  { id: "tile-1", kind: "session", sessionId: "s1" },
  { id: "tile-2", kind: "session", sessionId: "s2" },
  { id: "tile-3", kind: "session", sessionId: "s3" },
  { id: "tile-4", kind: "session", sessionId: "s4" },
];

describe("TileLayoutSwitcher", () => {
  it("renders a single cycle button on desktop that cycles 1 -> 2 -> 3 -> 4 -> 1", async () => {
    const user = userEvent.setup();
    const onSetCount = vi.fn();
    const { rerender } = render(
      <TileLayoutSwitcher
        tiles={TWO_TILES}
        threeOrientation="top"
        onSetCount={onSetCount}
        onFlipThree={vi.fn()}
      />,
    );

    const cycleBtn = screen.getByRole("button", {
      name: /Tile layout: 2 windows/i,
    });
    expect(cycleBtn).toBeInTheDocument();
    expect(cycleBtn).toHaveTextContent("2");
    expect(screen.queryByRole("button", { name: /1 window \(single focus\)/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /add tile/i })).toBeNull();

    await user.click(cycleBtn);
    expect(onSetCount).toHaveBeenCalledWith(3);

    rerender(
      <TileLayoutSwitcher
        tiles={FOUR_TILES}
        threeOrientation="top"
        onSetCount={onSetCount}
        onFlipThree={vi.fn()}
      />,
    );
    await user.click(screen.getByRole("button", { name: /Tile layout: 4 windows/i }));
    expect(onSetCount).toHaveBeenCalledWith(1);
  });

  it("shows the Flip button only when 3 tiles are open", async () => {
    const user = userEvent.setup();
    const onFlipThree = vi.fn();
    render(
      <TileLayoutSwitcher
        tiles={FOUR_TILES.slice(0, 3)}
        threeOrientation="top"
        onSetCount={vi.fn()}
        onFlipThree={onFlipThree}
      />,
    );

    const flipBtn = screen.getByRole("button", { name: "Flip 3-tile horizontal pane position" });
    await user.click(flipBtn);
    expect(onFlipThree).toHaveBeenCalledTimes(1);
  });

  it("renders dedicated MobileTileBar only when multiple tiles are open", async () => {
    const user = userEvent.setup();
    const onFocus = vi.fn();
    const onClose = vi.fn();
    const onAdd = vi.fn();

    const { rerender, container } = render(
      <MobileTileBar
        tiles={TWO_TILES.slice(0, 1)}
        focusedTileId="tile-1"
        focusedTitle="Only chat"
        canAdd
        onFocusTile={onFocus}
        onAddTile={onAdd}
        onCloseTile={onClose}
      />,
    );
    expect(container).toBeEmptyDOMElement();

    rerender(
      <MobileTileBar
        tiles={TWO_TILES}
        focusedTileId="tile-1"
        focusedTitle="First chat"
        canAdd
        onFocusTile={onFocus}
        onAddTile={onAdd}
        onCloseTile={onClose}
      />,
    );

    expect(screen.getByText("First chat")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Switch to tile 2" }));
    expect(onFocus).toHaveBeenCalledWith("tile-2");

    await user.click(screen.getByRole("button", { name: "Open another tile" }));
    expect(onAdd).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "Close active tile" }));
    expect(onClose).toHaveBeenCalledWith("tile-1");
  });
});
