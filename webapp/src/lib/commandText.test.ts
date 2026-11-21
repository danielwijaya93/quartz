import { describe, expect, it } from "vitest";
import { parseCommandText } from "./commandText";

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
