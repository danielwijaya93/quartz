import * as React from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CommandPalette } from "./CommandPalette";
import { Composer } from "./Composer";
import { useCommandPalette } from "@/lib/useCommandPalette";
import type { Command, ImageAttachment } from "@/protocol";

const COMMANDS: Command[] = [
  { name: "model", description: "Choose provider, model, and reasoning effort", usage: "[prov]" },
  { name: "compact", description: "Compact the conversation now", usage: "[tokens]" },
  { name: "jobs", description: "List background bash jobs" },
];

/** The parallel server PR: the flag is new, and only some commands carry it. */
const MIXED_COMMANDS: Command[] = [
  { name: "usage", description: "Show token and cost usage" },
  { name: "compact", description: "Compact the conversation now", usage: "[tokens]" },
  { name: "login", description: "Log in to a provider", usage: "[prov]", needsTerminal: true },
];

/**
 * The shipped wiring: `App` owns the composer ref and hands it to
 * `useCommandPalette`, which is what makes focus return to the composer. This
 * harness uses the same two pieces so the test covers the real path.
 */
function PaletteHarness({
  onRun,
  commands = COMMANDS,
}: {
  onRun: (name: string) => void;
  commands?: readonly Command[];
}): React.ReactElement {
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
        commands={commands}
        onOpenChange={palette.onOpenChange}
        onRun={onRun}
      />
    </div>
  );
}

/**
 * The whole `/` path, wired as `App` wires it: the composer, the palette and the
 * shared composer ref. A prompt that starts with a slash has to survive it.
 */
function SlashHarness({
  onSubmit,
  onRun,
}: {
  onSubmit: (text: string, images: ImageAttachment[]) => void;
  onRun: (name: string) => void;
}): React.ReactElement {
  const composerRef = React.useRef<HTMLTextAreaElement | null>(null);
  const palette = useCommandPalette(composerRef);

  return (
    <div>
      <Composer
        connection="connected"
        turnRunning={false}
        fatalError={null}
        onSubmit={onSubmit}
        onSteer={vi.fn()}
        onCancel={vi.fn()}
        onOpenPalette={palette.openPalette}
        textareaRef={composerRef}
      />
      <CommandPalette
        open={palette.open}
        commands={COMMANDS}
        onOpenChange={palette.onOpenChange}
        onRun={onRun}
      />
    </div>
  );
}

function renderHarness(commands?: readonly Command[]): { onRun: ReturnType<typeof vi.fn> } {
  const onRun = vi.fn();
  render(<PaletteHarness onRun={onRun} {...(commands ? { commands } : {})} />);
  return { onRun };
}

/** The command names on screen, in the order the palette drew them. */
function optionNames(): string[] {
  return screen
    .getAllByRole("option")
    .map((option) => option.id.replace("command-option-", ""));
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

  it("keeps an honest empty state", async () => {
    const user = userEvent.setup();
    renderHarness();
    await user.click(screen.getByRole("button", { name: "Open palette" }));
    const filter = await screen.findByRole("combobox", { name: "Filter commands" });
    await waitFor(() => expect(filter).toHaveFocus());

    await user.keyboard("qqqq");

    expect(screen.getByText("No command matches “qqqq”.")).toBeInTheDocument();
    expect(screen.queryAllByRole("option")).toEqual([]);
  });

  /* --------------------------------------------------------- terminal only */

  it("marks terminal-only commands and ranks them after the ones that work here", async () => {
    const user = userEvent.setup();
    renderHarness(MIXED_COMMANDS);
    await user.click(screen.getByRole("button", { name: "Open palette" }));
    await screen.findByRole("listbox", { name: "Available commands" });

    expect(optionNames()).toEqual(["compact", "usage", "login"]);
    const [compact, usage, login] = screen.getAllByRole("option");
    expect(within(login!).getByText("terminal")).toBeInTheDocument();
    expect(within(compact!).queryByText("terminal")).not.toBeInTheDocument();
    expect(within(usage!).queryByText("terminal")).not.toBeInTheDocument();
  });

  it("leaves the order and the badges alone when an older server omits the flag", async () => {
    const user = userEvent.setup();
    renderHarness([
      { name: "usage", description: "Show token and cost usage" },
      { name: "login", description: "Log in to a provider", usage: "[prov]" },
    ]);
    await user.click(screen.getByRole("button", { name: "Open palette" }));
    await screen.findByRole("listbox", { name: "Available commands" });

    // Absent means "works here": no badge, and the fuzzy ranking is untouched.
    expect(optionNames()).toEqual(["login", "usage"]);
    expect(screen.queryByText("terminal")).not.toBeInTheDocument();
  });

  it("still runs a terminal-only command so it can explain itself", async () => {
    const user = userEvent.setup();
    const { onRun } = renderHarness(MIXED_COMMANDS);
    await user.click(screen.getByRole("button", { name: "Open palette" }));
    const filter = await screen.findByRole("combobox", { name: "Filter commands" });
    await waitFor(() => expect(filter).toHaveFocus());

    await user.keyboard("login{Enter}");

    expect(onRun).toHaveBeenCalledWith("login");
  });

  /* ------------------------------------------------------------- usage hint */

  it("spells out the usage once an argument has been started", async () => {
    const user = userEvent.setup();
    const { onRun } = renderHarness(MIXED_COMMANDS);
    await user.click(screen.getByRole("button", { name: "Open palette" }));
    const filter = await screen.findByRole("combobox", { name: "Filter commands" });
    await waitFor(() => expect(filter).toHaveFocus());

    await user.keyboard("compact ");

    // The row is `/compact`, so `/compact 4000` matches nothing: the palette has
    // to say what the command takes instead of guessing for the person.
    expect(screen.getByText("Usage")).toBeInTheDocument();
    expect(screen.getByText("/compact [tokens]")).toBeInTheDocument();
    await user.keyboard("4000{Enter}");
    // It stays runnable as typed, argument and all.
    expect(onRun).toHaveBeenCalledWith("compact", "4000");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("says when the command whose argument is being typed needs a terminal", async () => {
    const user = userEvent.setup();
    renderHarness(MIXED_COMMANDS);
    await user.click(screen.getByRole("button", { name: "Open palette" }));
    const filter = await screen.findByRole("combobox", { name: "Filter commands" });
    await waitFor(() => expect(filter).toHaveFocus());

    await user.keyboard("login github ");

    expect(screen.getByText("(terminal only)")).toBeInTheDocument();
  });

  /* ----------------------------------------------------- the literal slash */

  it("gives a prompt that starts with a slash back to the composer", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    const onRun = vi.fn();
    render(<SlashHarness onSubmit={onSubmit} onRun={onRun} />);
    const composer = screen.getByRole("textbox", { name: "Prompt quartz" });
    await user.click(composer);

    await user.keyboard("/");
    // The affordance is unchanged: `/` on an empty composer opens the palette.
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    const filter = await screen.findByRole("combobox", { name: "Filter commands" });
    await waitFor(() => expect(filter).toHaveFocus());

    // A path cannot be a command, so the palette hands the text straight back.
    await user.keyboard("etc/");

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(composer).toHaveValue("/etc/");
    await waitFor(() => expect(composer).toHaveFocus());

    await user.keyboard("hosts says what?");
    expect(composer).toHaveValue("/etc/hosts says what?");

    await user.keyboard("{Enter}");
    expect(onSubmit).toHaveBeenCalledWith("/etc/hosts says what?", []);
    // Nothing was run as a command on the way through.
    expect(onRun).not.toHaveBeenCalled();
  });

  it("keeps a real command invocation in the palette", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    const onRun = vi.fn();
    render(<SlashHarness onSubmit={onSubmit} onRun={onRun} />);
    await user.click(screen.getByRole("textbox", { name: "Prompt quartz" }));

    await user.keyboard("/");
    const filter = await screen.findByRole("combobox", { name: "Filter commands" });
    await waitFor(() => expect(filter).toHaveFocus());
    await user.keyboard("compact 4000{Enter}");

    expect(onRun).toHaveBeenCalledWith("compact", "4000");
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("reopens the palette for a slash typed after Escape", async () => {
    const user = userEvent.setup();
    render(<SlashHarness onSubmit={vi.fn()} onRun={vi.fn()} />);
    const composer = screen.getByRole("textbox", { name: "Prompt quartz" });
    await user.click(composer);

    await user.keyboard("/");
    await screen.findByRole("dialog");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(composer).toHaveFocus());

    await user.keyboard("/");
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(composer).toHaveValue("");
  });
});
