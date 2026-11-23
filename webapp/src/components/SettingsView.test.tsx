import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SettingsView } from "./SettingsView";

describe("SettingsView", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders in-canvas settings without a modal dialog and returns via Back to chat", async () => {
    const user = userEvent.setup();
    const onBack = vi.fn();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          values: { reserve: 16384, keep: 32768, progress: 5, bg: 30 },
          defaults: { reserve: 16384, keep: 32768, progress: 5, bg: 30 },
          configPath: "~/.quartz/config.json",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    render(<SettingsView onBack={onBack} />);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(await screen.findByLabelText("Reserve tokens")).toHaveValue("16384");
    expect(screen.getByText("Stored in ~/.quartz/config.json")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Back to chat" }));
    expect(onBack).toHaveBeenCalledTimes(1);
  });
});
