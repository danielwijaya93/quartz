import { describe, expect, it } from "vitest";
import {
  handOffPrompt,
  isProseQuery,
  parseCommandText,
  PROMPT_HANDOFF_EVENT,
  readPromptHandoff,
} from "./commandText";

/** Shape of what `/help` actually returns: padded command columns, then keybindings. */
const HELP = [
  "/compact         Compact the conversation now",
  "/jobs            List background bash jobs",
  "/model [prov]    Choose provider, model, and reasoning effort",
  "/settings [key val]Show or change compaction settings",
  "/usage           Show token and cost usage",
  "",
  "Keybindings:",
  "  Enter            Submit prompt or steer running turn",
  "  Ctrl+J / \\+Enter Insert newline",
  "  Esc              Abort running turn or cancel modal/completion",
].join("\n");

describe("parseCommandText", () => {
  it("returns null for ordinary prose so it stays markdown", () => {
    expect(parseCommandText("You have used 1,234 tokens today.")).toBeNull();
  });

  it("returns null when only one line looks columnar", () => {
    expect(parseCommandText("Some intro\n/one   Only one row")).toBeNull();
  });

  it("splits /help into a command list and a keybinding list", () => {
    const parsed = parseCommandText(HELP);
    expect(parsed).not.toBeNull();
    const [commands, keybindings] = parsed?.sections ?? [];
    expect(commands?.heading).toBeNull();
    expect(commands?.rows[0]).toEqual({ label: "/compact", detail: "Compact the conversation now" });
    expect(commands?.rows).toHaveLength(5);
    expect(keybindings?.heading).toBe("Keybindings");
    expect(keybindings?.rows.map((row) => row.label)).toContain("Esc");
  });

  it("recovers the rows whose padded column overflowed", () => {
    const rows = parseCommandText(HELP)?.sections[0]?.rows ?? [];
    expect(rows.find((row) => row.label === "/settings [key val]")?.detail).toBe(
      "Show or change compaction settings",
    );
  });

  it("keeps a non-columnar line rather than dropping it", () => {
    const parsed = parseCommandText(HELP);
    const labels = parsed?.sections.flatMap((section) => section.rows.map((row) => row.label)) ?? [];
    expect(labels).toContain("Ctrl+J / \\+Enter");
  });
});

/** The names the palette would offer, in the order a server sends them. */
const COMMAND_NAMES = ["model", "compact", "jobs", "settings", "usage"];

describe("isProseQuery", () => {
  it("keeps a command being typed", () => {
    expect(isProseQuery("", COMMAND_NAMES)).toBe(false);
    expect(isProseQuery("comp", COMMAND_NAMES)).toBe(false);
    expect(isProseQuery("compact", COMMAND_NAMES)).toBe(false);
    expect(isProseQuery("Compact", COMMAND_NAMES)).toBe(false);
  });

  it("keeps an argument after a real command", () => {
    expect(isProseQuery("compact 4000", COMMAND_NAMES)).toBe(false);
    expect(isProseQuery("settings progress 5", COMMAND_NAMES)).toBe(false);
  });

  it("keeps a half-typed name no command matches yet", () => {
    // Nothing is wrong with "zzz" as a name; the list is simply empty so far.
    expect(isProseQuery("zzz", COMMAND_NAMES)).toBe(false);
  });

  it("gives up on a space after a word that is not a command", () => {
    expect(isProseQuery("zzz ", COMMAND_NAMES)).toBe(true);
    expect(isProseQuery("zzz says hello", COMMAND_NAMES)).toBe(true);
    expect(isProseQuery(" ", COMMAND_NAMES)).toBe(true);
  });

  it("gives up on a character a command name cannot hold", () => {
    expect(isProseQuery("etc/", COMMAND_NAMES)).toBe(true);
    expect(isProseQuery("etc/hosts", COMMAND_NAMES)).toBe(true);
    expect(isProseQuery("^a+b$", COMMAND_NAMES)).toBe(true);
    expect(isProseQuery("jobs/x", COMMAND_NAMES)).toBe(true);
  });
});

describe("the prompt handoff", () => {
  it("carries the filter text back with the slash that opened the palette", () => {
    const seen: string[] = [];
    const listener = (event: Event): void => {
      const text = readPromptHandoff(event);
      if (text !== null) seen.push(text);
    };
    window.addEventListener(PROMPT_HANDOFF_EVENT, listener);
    handOffPrompt("etc/");
    window.removeEventListener(PROMPT_HANDOFF_EVENT, listener);
    expect(seen).toEqual(["/etc/"]);
  });

  it("ignores anything that is not text on the channel", () => {
    expect(readPromptHandoff(new Event(PROMPT_HANDOFF_EVENT))).toBeNull();
    expect(readPromptHandoff(new CustomEvent(PROMPT_HANDOFF_EVENT, { detail: 42 }))).toBeNull();
  });
});
