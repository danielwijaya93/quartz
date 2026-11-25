import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MobileTileBar, TileLayoutSwitcher } from "./TileLayoutSwitcher";
import type { TileDescriptor } from "@/lib/tiling";

const TWO_TILES: readonly TileDescriptor[] = [
  { id: "tile-1", kind: "session", sessionId: "s1" },
  { id: "tile-2", kind: "session", sessionId: "s2" },
];

describe("TileLayoutSwitcher", () => {
  it("renders 1-4 layout controls on desktop without a redundant + button", async () => {
    const user = userEvent.setup();
    const onSetCount = vi.fn();
    render(
      <TileLayoutSwitcher
        tiles={TWO_TILES}
        threeOrientation="top"
        onSetCount={onSetCount}
        onFlipThree={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: "1 window (single focus)" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "2 windows (vertical split)" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "3 windows (1 wide + 2 vertical)" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "4 windows (2x2 grid)" })).toBeInTheDocument();
    // No duplicate '+' / 'Add tile' button next to '+ New' in the header
    expect(screen.queryByRole("button", { name: /add tile/i })).toBeNull();

    await user.click(screen.getByRole("button", { name: "3 windows (1 wide + 2 vertical)" }));
    expect(onSetCount).toHaveBeenCalledWith(3);
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
