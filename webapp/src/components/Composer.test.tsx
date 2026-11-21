import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Composer } from "./Composer";

function setup(overrides: Partial<React.ComponentProps<typeof Composer>> = {}) {
  const onSubmit = vi.fn();
  const onSteer = vi.fn();
  const onCancel = vi.fn();
  const onOpenPalette = vi.fn();
  render(
    <Composer
      connection="connected"
      turnRunning={false}
      fatalError={null}
      onSubmit={onSubmit}
      onSteer={onSteer}
      onCancel={onCancel}
      onOpenPalette={onOpenPalette}
      {...overrides}
    />,
  );
  return { onSubmit, onSteer, onCancel, onOpenPalette };
}

describe("Composer", () => {
  it("submits on Enter and clears the draft", async () => {
    const user = userEvent.setup();
    const { onSubmit } = setup();
    const textarea = screen.getByRole("textbox", { name: "Prompt quartz" });
    await user.click(textarea);
    await user.keyboard("hello{Enter}");
    expect(onSubmit).toHaveBeenCalledWith("hello", []);
    expect(textarea).toHaveValue("");
  });

  it("inserts a newline on Shift+Enter instead of submitting", async () => {
    const user = userEvent.setup();
    const { onSubmit } = setup();
    const textarea = screen.getByRole("textbox", { name: "Prompt quartz" });
    await user.click(textarea);
    await user.keyboard("line one{Shift>}{Enter}{/Shift}line two");
    expect(onSubmit).not.toHaveBeenCalled();
    expect(textarea).toHaveValue("line one\nline two");
  });

  it("steers instead of prompting while a turn runs", async () => {
    const user = userEvent.setup();
    const { onSubmit, onSteer } = setup({ turnRunning: true });
    const textarea = screen.getByRole("textbox", { name: "Steer the running turn" });
    await user.click(textarea);
    await user.keyboard("turn left{Enter}");
    expect(onSteer).toHaveBeenCalledWith("turn left");
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("labels the button Steer while a turn runs", () => {
    setup({ turnRunning: true });
    expect(screen.getByRole("button", { name: "Steer the running turn" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel the running turn" })).toBeInTheDocument();
  });

  it("cancels the running turn on Escape", async () => {
    const user = userEvent.setup();
    const { onCancel } = setup({ turnRunning: true });
    const textarea = screen.getByRole("textbox", { name: "Steer the running turn" });
    await user.click(textarea);
    await user.keyboard("{Escape}");
    expect(onCancel).toHaveBeenCalled();
  });

  it("clears the draft on Escape when idle", async () => {
    const user = userEvent.setup();
    setup();
    const textarea = screen.getByRole("textbox", { name: "Prompt quartz" });
    await user.click(textarea);
    await user.keyboard("draft{Escape}");
    expect(textarea).toHaveValue("");
  });

  it("opens the palette when / is typed into an empty draft", async () => {
    const user = userEvent.setup();
    const { onOpenPalette } = setup();
    const textarea = screen.getByRole("textbox", { name: "Prompt quartz" });
    await user.click(textarea);
    await user.keyboard("/");
    expect(onOpenPalette).toHaveBeenCalled();
    expect(textarea).toHaveValue("");
  });

  it("recalls the previous prompt on ArrowUp", async () => {
    const user = userEvent.setup();
    setup();
    const textarea = screen.getByRole("textbox", { name: "Prompt quartz" });
    await user.click(textarea);
    await user.keyboard("first{Enter}");
    await user.keyboard("{ArrowUp}");
    expect(textarea).toHaveValue("first");
  });

  it("is disabled and explains why when disconnected", () => {
    setup({ connection: "disconnected" });
    const textarea = screen.getByRole("textbox", { name: "Prompt quartz" });
    expect(textarea).toBeDisabled();
    expect(textarea).toHaveAttribute("placeholder", expect.stringContaining("Disconnected"));
  });

  it("blocks input after a fatal error", () => {
    setup({ fatalError: "auth failed" });
    expect(screen.getByRole("textbox")).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("auth failed");
  });

  it("attaches an image and sends it with the prompt", async () => {
    const user = userEvent.setup();
    const { onSubmit } = setup();
    const file = new File([new Uint8Array([137, 80, 78, 71])], "x.png", { type: "image/png" });
    const input = screen.getByLabelText("Attach images");
    await user.upload(input, file);
    expect(await screen.findByAltText("Pending attachment 1")).toBeInTheDocument();

    const textarea = screen.getByRole("textbox", { name: "Prompt quartz" });
    await user.click(textarea);
    await user.keyboard("look{Enter}");
    expect(onSubmit).toHaveBeenCalledTimes(1);
    const [text, images] = onSubmit.mock.calls[0] as [string, { mimeType: string; data: string }[]];
    expect(text).toBe("look");
    expect(images).toHaveLength(1);
    expect(images[0]?.mimeType).toBe("image/png");
    // Base64 payload, no `data:` prefix: what the protocol asks for.
    expect(images[0]?.data).not.toContain("data:");
  });
});
