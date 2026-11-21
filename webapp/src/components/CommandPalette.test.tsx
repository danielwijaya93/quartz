import * as React from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CommandPalette } from "./CommandPalette";
import { useCommandPalette } from "@/lib/useCommandPalette";
import type { Command } from "@/protocol";

const COMMANDS: Command[] = [
  { name: "model", description: "Choose provider, model, and reasoning effort", usage: "[prov]" },
  { name: "compact", description: "Compact the conversation now", usage: "[tokens]" },
  { name: "jobs", description: "List background bash jobs" },
];

/**
 * The shipped wiring: `App` owns the composer ref and hands it to
 * `useCommandPalette`, which is what makes focus return to the composer. This
 * harness uses the same two pieces so the test covers the real path.
 */
function PaletteHarness({ onRun }: { onRun: (name: string) => void }): React.ReactElement {
  const composerRef = React.useRef<HTMLTextAreaElement | null>(null);
  const palette = useCommandPalette(composerRef);

  return (
    <div>
      <textarea ref={composerRef} aria-label="Prompt quartz" />
      <button type="button" onClick={palette.openPalette}>
        Open palette
      </button>
      <CommandPalette
        open={palette.open}
        commands={COMMANDS}
        onOpenChange={palette.onOpenChange}
        onRun={onRun}
      />
    </div>
  );
}

function renderHarness(): { onRun: ReturnType<typeof vi.fn> } {
  const onRun = vi.fn();
  render(<PaletteHarness onRun={onRun} />);
  return { onRun };
}

describe("CommandPalette", () => {
  it("opens as a labelled dialog and focuses the filter", async () => {
    const user = userEvent.setup();
    renderHarness();
    await user.click(screen.getByRole("button", { name: "Open palette" }));

    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveAccessibleName("Commands");
    await waitFor(() => {
      expect(screen.getByRole("combobox", { name: "Filter commands" })).toHaveFocus();
    });
  });

  it("returns focus to the composer when closed with Escape", async () => {
    const user = userEvent.setup();
    renderHarness();
    const composer = screen.getByRole("textbox", { name: "Prompt quartz" });

    await user.click(screen.getByRole("button", { name: "Open palette" }));
    const filter = await screen.findByRole("combobox", { name: "Filter commands" });
    await waitFor(() => expect(filter).toHaveFocus());

    await user.keyboard("{Escape}");

    await waitFor(() => expect(composer).toHaveFocus());
  });

  it("filters, then runs the highlighted command on Enter", async () => {
    const user = userEvent.setup();
    const { onRun } = renderHarness();

    await user.click(screen.getByRole("button", { name: "Open palette" }));
    const filter = await screen.findByRole("combobox", { name: "Filter commands" });
    await waitFor(() => expect(filter).toHaveFocus());

    await user.keyboard("jobs{Enter}");
    expect(onRun).toHaveBeenCalledWith("jobs");
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Prompt quartz" })).toHaveFocus());
  });

  it("shows usage hints and descriptions for every command", async () => {
    const user = userEvent.setup();
    renderHarness();
    await user.click(screen.getByRole("button", { name: "Open palette" }));

    expect(await screen.findByText("/compact")).toBeInTheDocument();
    expect(screen.getByText("[tokens]")).toBeInTheDocument();
    expect(screen.getByText("Compact the conversation now")).toBeInTheDocument();
    expect(screen.getByRole("listbox", { name: "Available commands" })).toBeInTheDocument();
  });
});
