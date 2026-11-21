import { describe, expect, it } from "vitest";
import { filterCommands, fuzzyScore, parseCommandInput } from "./CommandPalette";
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
