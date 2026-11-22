import { describe, expect, it } from "vitest";
import {
  filterCommands,
  fuzzyScore,
  isTerminalOnly,
  orderCommands,
  parseCommandInput,
  usageHint,
} from "./CommandPalette";
import type { Command } from "@/protocol";

const COMMANDS: Command[] = [
  { name: "model", description: "Choose provider, model, and reasoning effort", usage: "[prov]" },
  { name: "compact", description: "Compact the conversation now", usage: "[tokens]" },
  { name: "jobs", description: "List background bash jobs" },
  { name: "usage", description: "Show token and cost usage" },
  { name: "settings", description: "Show or change compaction settings", usage: "[key val]" },
];

describe("fuzzyScore", () => {
  it("scores an empty query as a neutral match", () => {
    expect(fuzzyScore("", "anything")).toBe(0);
  });

  it("ranks prefix above substring above subsequence", () => {
    const prefix = fuzzyScore("mo", "model");
    const substring = fuzzyScore("del", "model");
    const subsequence = fuzzyScore("mdl", "model");
    expect(prefix).toBeGreaterThan(substring);
    expect(substring).toBeGreaterThan(subsequence);
  });

  it("returns -1 when the query does not match at all", () => {
    expect(fuzzyScore("zzz", "model")).toBe(-1);
  });
});

describe("filterCommands", () => {
  it("returns everything for an empty query, alphabetically", () => {
    expect(filterCommands(COMMANDS, "").map((c) => c.name)).toEqual([
      "compact",
      "jobs",
      "model",
      "settings",
      "usage",
    ]);
  });

  it("ranks a name match above a description-only match", () => {
    const names = filterCommands(COMMANDS, "comp").map((c) => c.name);
    // "settings" mentions compaction in its description, so it legitimately
    // matches too — but "compact" is the prefix hit and must come first.
    expect(names[0]).toBe("compact");
    expect(names).toContain("settings");
  });

  it("matches on the description too", () => {
    const names = filterCommands(COMMANDS, "bash").map((c) => c.name);
    expect(names).toContain("jobs");
  });

  it("returns an empty list for no match", () => {
    expect(filterCommands(COMMANDS, "qqqq")).toEqual([]);
  });
});

describe("parseCommandInput", () => {
  it("splits a bare command", () => {
    expect(parseCommandInput("/jobs")).toEqual({ name: "jobs", arg: "" });
  });

  it("splits a command with an argument", () => {
    expect(parseCommandInput("/settings progress 5")).toEqual({
      name: "settings",
      arg: "progress 5",
    });
  });

  it("tolerates input without a leading slash", () => {
    expect(parseCommandInput("compact 4000")).toEqual({ name: "compact", arg: "4000" });
  });
});

describe("isTerminalOnly", () => {
  it("reads the server's flag", () => {
    expect(isTerminalOnly({ name: "login", description: "", needsTerminal: true })).toBe(true);
    expect(isTerminalOnly({ name: "usage", description: "", needsTerminal: false })).toBe(false);
  });

  it("treats an absent flag as a command that works here", () => {
    // An older server does not send the field; burying every command because of
    // it would be worse than the bug this marks.
    expect(isTerminalOnly({ name: "usage", description: "" })).toBe(false);
  });
});

describe("orderCommands", () => {
  const MIXED: Command[] = [
    { name: "login", description: "Log in to a provider", needsTerminal: true },
    { name: "usage", description: "Show token and cost usage" },
    { name: "model", description: "Choose provider and model", needsTerminal: true },
    { name: "compact", description: "Compact the conversation now" },
  ];

  it("keeps the ranking inside each group and sinks the terminal-only ones", () => {
    expect(orderCommands(MIXED).map((command) => command.name)).toEqual([
      "usage",
      "compact",
      "login",
      "model",
    ]);
  });

  it("leaves the order alone when no command carries the flag", () => {
    const plain = MIXED.map(({ name, description }) => ({ name, description }));
    expect(orderCommands(plain).map((command) => command.name)).toEqual(
      plain.map((command) => command.name),
    );
  });

  it("does not mutate the list it was given", () => {
    const names = MIXED.map((command) => command.name);
    orderCommands(MIXED);
    expect(MIXED.map((command) => command.name)).toEqual(names);
  });
});

describe("usageHint", () => {
  const COMMANDS: Command[] = [
    { name: "compact", description: "Compact the conversation now", usage: "[tokens]" },
    { name: "login", description: "Log in", usage: "[prov]", needsTerminal: true },
  ];

  it("names the command once an argument has been started", () => {
    expect(usageHint(COMMANDS, "compact 4000")?.name).toBe("compact");
    expect(usageHint(COMMANDS, "compact ")?.usage).toBe("[tokens]");
  });

  it("is silent while only the name is typed", () => {
    expect(usageHint(COMMANDS, "compact")).toBeNull();
    expect(usageHint(COMMANDS, "")).toBeNull();
  });

  it("has nothing to say about a word that is not a command", () => {
    expect(usageHint(COMMANDS, "zzz 4000")).toBeNull();
  });

  it("carries the terminal flag through, so the hint can say so", () => {
    expect(usageHint(COMMANDS, "login github")?.needsTerminal).toBe(true);
  });
});
