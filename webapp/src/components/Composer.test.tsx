import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { handOffPrompt } from "@/lib/commandText";
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
    const input = screen.getByLabelText("File upload");
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

  it("takes a slash draft back from the palette, caret at the end", async () => {
    const user = userEvent.setup();
    const { onSubmit } = setup();
    const textarea = screen.getByRole("textbox", { name: "Prompt quartz" });
    await user.click(textarea);

    // The palette decides this text was never a command and hands it back whole.
    handOffPrompt("etc/");

    await waitFor(() => expect(textarea).toHaveValue("/etc/"));
    expect(textarea).toHaveFocus();
    expect((textarea as HTMLTextAreaElement).selectionStart).toBe(5);

    await user.keyboard("hosts says what?{Enter}");
    expect(onSubmit).toHaveBeenCalledWith("/etc/hosts says what?", []);
  });

  it("appends the handed-back text to a draft that already spans lines", async () => {
    const user = userEvent.setup();
    setup();
    const textarea = screen.getByRole("textbox", { name: "Prompt quartz" });
    await user.click(textarea);
    await user.keyboard("first{Shift>}{Enter}{/Shift}");

    handOffPrompt("etc/");

    await waitFor(() => expect(textarea).toHaveValue("first\n/etc/"));
  });

  it("never opens the palette when a pasted draft starts with a slash", async () => {
    const user = userEvent.setup();
    const { onOpenPalette } = setup();
    const textarea = screen.getByRole("textbox", { name: "Prompt quartz" });

    await user.click(textarea);
    await user.paste("/etc/hosts says what?");

    expect(textarea).toHaveValue("/etc/hosts says what?");
    expect(onOpenPalette).not.toHaveBeenCalled();
  });

  it("opens the palette again for a slash typed after Escape", async () => {
    const user = userEvent.setup();
    const { onOpenPalette } = setup();
    const textarea = screen.getByRole("textbox", { name: "Prompt quartz" });
    await user.click(textarea);

    await user.keyboard("/");
    expect(onOpenPalette).toHaveBeenCalledTimes(1);

    await user.keyboard("{Escape}");
    await user.keyboard("/");
    // Nothing was left behind in the field, so the affordance still works.
    expect(onOpenPalette).toHaveBeenCalledTimes(2);
  });

  it("does not open the palette for a slash inside a draft", async () => {
    const user = userEvent.setup();
    const { onOpenPalette } = setup();
    const textarea = screen.getByRole("textbox", { name: "Prompt quartz" });
    await user.click(textarea);

    await user.keyboard("a / b");
    expect(textarea).toHaveValue("a / b");
    expect(onOpenPalette).not.toHaveBeenCalled();
  });

  it("shows inline slash commands when commands are provided and runs the selected command", async () => {
    const user = userEvent.setup();
    const onRunCommand = vi.fn();
    setup({
      commands: [
        { name: "help", description: "Show available commands" },
        { name: "compact", description: "Summarize older messages", usage: "[focus]" },
      ],
      onRunCommand,
    });
    const textarea = screen.getByRole("textbox", { name: "Prompt quartz" });
    await user.click(textarea);
    await user.keyboard("/");

    expect(screen.getByRole("listbox", { name: "Available commands" })).toBeInTheDocument();
    expect(screen.getByText("/help")).toBeInTheDocument();
    expect(screen.getByText("/compact")).toBeInTheDocument();

    await user.keyboard("he{Enter}");
    expect(onRunCommand).toHaveBeenCalledWith("help", undefined);
    expect(textarea).toHaveValue("");
  });

  it("omits the slash button and Enter/Shift+Enter hints", () => {
    setup();
    expect(screen.queryByRole("button", { name: "Slash commands" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Shift\+Enter/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/to send/i)).not.toBeInTheDocument();
  });

  it("attaches a non-image file like csv and sends its contents with the prompt", async () => {
    const user = userEvent.setup();
    const { onSubmit } = setup();
    const csv = new File(["a,b\n1,2"], "data.csv", { type: "text/csv" });
    const input = screen.getByLabelText("File upload");
    await user.upload(input, csv);
    expect(await screen.findByText("data.csv")).toBeInTheDocument();

    const textarea = screen.getByRole("textbox", { name: "Prompt quartz" });
    await user.click(textarea);
    await user.keyboard("summarize{Enter}");
    expect(onSubmit).toHaveBeenCalledTimes(1);
    const [text, images] = onSubmit.mock.calls[0] as [string, unknown[]];
    expect(text).toContain("summarize");
    expect(text).toContain("[Attached file: data.csv]");
    expect(text).toContain("a,b");
    expect(images).toEqual([]);
  });

  it("shows model and short effort label and opens model picker on click", async () => {
    const user = userEvent.setup();
    const onOpenModelPicker = vi.fn();
    setup({
      model: "gemini-4-hydrogen",
      effort: "medium",
      onOpenModelPicker,
    });
    const btn = screen.getByRole("button", {
      name: "Switch model (current: gemini-4-hydrogen Med)",
    });
    expect(btn).toHaveTextContent("gemini-4-hydrogen");
    expect(btn).toHaveTextContent("Med");
    await user.click(btn);
    expect(onOpenModelPicker).toHaveBeenCalledTimes(1);
  });

  it("renders the context usage ring with percentage and window tooltip", () => {
    setup({
      model: "gemini-4-hydrogen",
      effort: "medium",
      contextPct: 23.5,
      maxContextTokens: 1_000_000,
      onOpenModelPicker: vi.fn(),
    });
    expect(screen.getByRole("img", { name: "Context: 23.5% of 1.0M" })).toBeInTheDocument();
  });

  it("runs /reset via onRunCommand", async () => {
    const user = userEvent.setup();
    const onRunCommand = vi.fn();
    const { onSubmit } = setup({
      commands: [
        { name: "help", description: "Show available commands" },
        { name: "reset", description: "Reset the kernel to a clean namespace" },
      ],
      onRunCommand,
    });
    const textarea = screen.getByRole("textbox", { name: "Prompt quartz" });
    await user.click(textarea);
    await user.keyboard("/reset{Enter}");
    expect(onRunCommand).toHaveBeenCalledWith("reset", undefined);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(textarea).toHaveValue("");
  });

  it("runs a known command with arguments via onRunCommand", async () => {
    const user = userEvent.setup();
    const onRunCommand = vi.fn();
    const { onSubmit } = setup({
      commands: [{ name: "compact", description: "Compact the conversation now", usage: "[tokens]" }],
      onRunCommand,
    });
    const textarea = screen.getByRole("textbox", { name: "Prompt quartz" });
    await user.click(textarea);
    await user.keyboard("/compact 4000{Enter}");
    expect(onRunCommand).toHaveBeenCalledWith("compact", "4000");
    expect(onSubmit).not.toHaveBeenCalled();
    expect(textarea).toHaveValue("");
  });

  it("sends unknown /command prompts as text instead of misrouting to onRunCommand", async () => {
    const user = userEvent.setup();
    const onRunCommand = vi.fn();
    const { onSubmit } = setup({
      commands: [
        { name: "help", description: "Show available commands" },
        { name: "reset", description: "Reset the kernel" },
      ],
      onRunCommand,
    });
    const textarea = screen.getByRole("textbox", { name: "Prompt quartz" });
    await user.click(textarea);
    await user.keyboard("/unknownword{Enter}");
    expect(onRunCommand).not.toHaveBeenCalled();
    expect(onSubmit).toHaveBeenCalledWith("/unknownword", []);
    expect(textarea).toHaveValue("");
  });

  it("steers unknown /command prompts when a turn is running", async () => {
    const user = userEvent.setup();
    const onRunCommand = vi.fn();
    const { onSteer } = setup({
      turnRunning: true,
      commands: [{ name: "help", description: "Show available commands" }],
      onRunCommand,
    });
    const textarea = screen.getByRole("textbox", { name: "Steer the running turn" });
    await user.click(textarea);
    await user.keyboard("/unknownword{Enter}");
    expect(onRunCommand).not.toHaveBeenCalled();
    expect(onSteer).toHaveBeenCalledWith("/unknownword");
    expect(textarea).toHaveValue("");
  });

  it("preserves typed arguments when another command matches the full query", async () => {
    const user = userEvent.setup();
    const onRunCommand = vi.fn();
    const { onSubmit } = setup({
      commands: [
        { name: "reset", description: "Clear the kernel namespace" },
        { name: "image", description: "Attach an image file", usage: "<path|clear>" },
        { name: "expand", description: "Show full output", usage: "[n]" },
      ],
      onRunCommand,
    });
    const textarea = screen.getByRole("textbox", { name: "Prompt quartz" });
    await user.click(textarea);
    await user.keyboard("/image clear{Enter}");
    expect(onRunCommand).toHaveBeenCalledWith("image", "clear");
    expect(onSubmit).not.toHaveBeenCalled();
    expect(textarea).toHaveValue("");
  });

  it("blocks oversized payloads exceeding MAX_FRAME_BYTES without clearing draft or attachments", async () => {
    const user = userEvent.setup();
    const { onSubmit } = setup();
    const bigBytes = new Uint8Array(800 * 1024);
    const file = new File([bigBytes], "huge.png", { type: "image/png" });
    const input = screen.getByLabelText("File upload");
    await user.upload(input, file);
    expect(await screen.findByAltText("Pending attachment 1")).toBeInTheDocument();

    const textarea = screen.getByRole("textbox", { name: "Prompt quartz" });
    await user.click(textarea);
    await user.keyboard("keep me{Enter}");

    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("1 MiB");
    expect(textarea).toHaveValue("keep me");
    expect(screen.getByAltText("Pending attachment 1")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Remove attachment 1" }));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("opens @file autocomplete on @, filters files, and inserts @path on Tab without submitting", async () => {
    const user = userEvent.setup();
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          cwd: "/repo",
          files: ["README.md", "src/components/Composer.tsx", "src/App.tsx"],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    const { onSubmit } = setup({ cwd: "/repo" });
    const textarea = screen.getByRole("textbox", { name: "Prompt quartz" });
    await user.click(textarea);
    await user.keyboard("check @comp");

    const listbox = await screen.findByRole("listbox", { name: "Matching workspace files" });
    expect(listbox).toBeInTheDocument();
    expect(screen.getByText("src/components/Composer.tsx")).toBeInTheDocument();

    await user.keyboard("{Tab}");
    expect(textarea).toHaveValue("check @src/components/Composer.tsx ");
    expect(onSubmit).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });
});
