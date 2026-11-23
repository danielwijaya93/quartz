import { describe, expect, it } from "vitest";
import { detectAtMention, filterMentionFiles, insertAtMention } from "./fileMentions";

describe("detectAtMention", () => {
  it("detects @ at start of text or after whitespace", () => {
    expect(detectAtMention("@comp", 5)).toEqual({ start: 0, end: 5, query: "comp" });
    expect(detectAtMention("check @src/app", 14)).toEqual({
      start: 6,
      end: 14,
      query: "src/app",
    });
    expect(detectAtMention("@", 1)).toEqual({ start: 0, end: 1, query: "" });
  });

  it("ignores email addresses and tokens with whitespace after @", () => {
    expect(detectAtMention("user@example.com", 16)).toBeNull();
    expect(detectAtMention("@foo bar", 8)).toBeNull();
  });
});

describe("filterMentionFiles", () => {
  const files = [
    "README.md",
    "src/components/Composer.tsx",
    "src/components/HeaderBar.tsx",
    "src/App.tsx",
    "tests/composer.test.ts",
  ];

  it("ranks basename prefix matches before substring and fuzzy matches", () => {
    expect(filterMentionFiles(files, "comp")).toEqual([
      "tests/composer.test.ts",
      "src/components/Composer.tsx",
      "src/components/HeaderBar.tsx",
    ]);
  });

  it("supports fuzzy subsequence matching", () => {
    expect(filterMentionFiles(files, "hbr")).toContain("src/components/HeaderBar.tsx");
  });
});

describe("insertAtMention", () => {
  it("replaces @query with @filePath and a trailing space", () => {
    const text = "inspect @comp please";
    const mention = detectAtMention(text, 13)!;
    expect(insertAtMention(text, mention, "src/components/Composer.tsx")).toEqual({
      nextText: "inspect @src/components/Composer.tsx  please",
      nextCaret: 37,
    });
  });
});
