import * as React from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SidebarDrawer } from "./SidebarDrawer";

function Harness(): React.ReactElement {
  const [open, setOpen] = React.useState(true);
  return (
    <div>
      <button type="button" onClick={() => setOpen(true)}>
        Show sessions
      </button>
      {open ? (
        <SidebarDrawer onClose={() => setOpen(false)}>
          <nav aria-label="Sessions">
            <button type="button">a row</button>
          </nav>
        </SidebarDrawer>
      ) : null}
    </div>
  );
}

describe("SidebarDrawer", () => {
  it("closes on Escape", async () => {
    const user = userEvent.setup();
    render(<Harness />);

    expect(screen.getByRole("navigation", { name: "Sessions" })).toBeInTheDocument();
    await user.keyboard("{Escape}");

    await waitFor(() => {
      expect(screen.queryByRole("navigation", { name: "Sessions" })).not.toBeInTheDocument();
    });
  });

  it("closes when the backdrop is tapped, which is how a thumb dismisses it", async () => {
    const onClose = vi.fn();
    render(
      <SidebarDrawer onClose={onClose}>
        <nav aria-label="Sessions" />
      </SidebarDrawer>,
    );

    const backdrop = document.querySelector(".bg-black\\/50");
    expect(backdrop).not.toBeNull();
    await userEvent.setup().click(backdrop as Element);

    expect(onClose).toHaveBeenCalled();
  });

  it("moves focus into the panel and gives it back when it closes", async () => {
    const user = userEvent.setup();
    render(<Harness />);

    const opener = screen.getByRole("button", { name: "Show sessions" });
    opener.focus();

    // It was already open, so re-open it to exercise the focus path.
    await user.keyboard("{Escape}");
    await user.click(opener);

    const row = await screen.findByRole("button", { name: "a row" });
    expect(row).toBeInTheDocument();
    await user.keyboard("{Escape}");

    await waitFor(() => expect(opener).toHaveFocus());
  });
});
