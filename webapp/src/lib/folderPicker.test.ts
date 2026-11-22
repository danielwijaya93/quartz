import { describe, expect, it } from "vitest";
import {
  choiceForCreated,
  choiceForListing,
  currentName,
  filterDirectories,
  highlightedEntry,
  indexOfEntry,
  parentPath,
  parseListing,
  stepHighlight,
  type DirectoryListing,
} from "./folderPicker";

function listing(overrides: Partial<DirectoryListing> = {}): DirectoryListing {
  return {
    path: "/home/x",
    parents: [
      { name: "/", path: "/" },
      { name: "home", path: "/home" },
      { name: "x", path: "/home/x" },
    ],
    entries: [
      { name: "alpha", path: "/home/x/alpha", isDir: true },
      { name: "beta-project", path: "/home/x/beta-project", isDir: true },
      { name: "gamma", path: "/home/x/gamma", isDir: true },
    ],
    error: null,
    ...overrides,
  };
}

describe("filterDirectories", () => {
  it("narrows the current listing by substring", () => {
    expect(filterDirectories(listing().entries, "et").map((entry) => entry.name)).toEqual([
      "beta-project",
    ]);
  });

  it("is case-insensitive and ignores surrounding space", () => {
    expect(filterDirectories(listing().entries, " ALP ").map((e) => e.name)).toEqual(["alpha"]);
  });

  it("keeps everything for an empty query", () => {
    expect(filterDirectories(listing().entries, "   ")).toHaveLength(3);
  });

  it("never walks a path: a slash matches nothing", () => {
    expect(filterDirectories(listing().entries, "../beta")).toEqual([]);
    expect(filterDirectories(listing().entries, "/home/x/alpha")).toEqual([]);
  });
});

describe("breadcrumbs", () => {
  it("finds the parent from the listing's own crumbs", () => {
    expect(parentPath(listing())).toBe("/home");
  });

  it("has no parent at the root", () => {
    expect(parentPath(listing({ parents: [{ name: "/", path: "/" }] }))).toBeNull();
    expect(parentPath(listing({ parents: [] }))).toBeNull();
  });

  it("names the directory on screen", () => {
    expect(currentName(listing())).toBe("x");
    expect(currentName(listing({ parents: [], path: "/home/x" }))).toBe("x");
    expect(currentName(listing({ parents: [], path: "/" }))).toBe("/");
  });
});

describe("parseListing", () => {
  it("reads a listing and keeps only directories", () => {
    const parsed = parseListing({
      path: "/home/x",
      parents: [{ name: "home", path: "/home" }],
      entries: [
        { name: "alpha", path: "/home/x/alpha", isDir: true },
        { name: "notes.txt", path: "/home/x/notes.txt", isDir: false },
        { name: "", path: "", isDir: true },
      ],
      error: null,
    });
    expect(parsed.entries.map((entry) => entry.name)).toEqual(["alpha"]);
    expect(parsed.error).toBeNull();
  });

  it("carries an error value through instead of throwing", () => {
    const parsed = parseListing({ path: "/nope", parents: [], entries: [], error: "not a directory" });
    expect(parsed.error).toBe("not a directory");
  });

  it("throws on a body that is not a listing at all", () => {
    expect(() => parseListing({ paths: [] })).toThrow(/Malformed/);
    expect(() => parseListing("nope")).toThrow(/Malformed/);
  });
});

describe("stepHighlight", () => {
  it("walks down and up through the rows", () => {
    expect(stepHighlight(0, 3, 1)).toBe(1);
    expect(stepHighlight(2, 3, -1)).toBe(1);
  });

  it("wraps at both ends rather than dying there", () => {
    expect(stepHighlight(2, 3, 1)).toBe(0);
    expect(stepHighlight(0, 3, -1)).toBe(2);
  });

  it("starts at the first row going down and the last going up", () => {
    expect(stepHighlight(-1, 3, 1)).toBe(0);
    expect(stepHighlight(-1, 3, -1)).toBe(2);
  });

  it("has nowhere to go in an empty list", () => {
    expect(stepHighlight(-1, 0, 1)).toBe(-1);
    expect(stepHighlight(0, 0, 1)).toBe(-1);
  });

  it("recovers from an index the list no longer has", () => {
    expect(stepHighlight(9, 3, 1)).toBe(0);
    expect(stepHighlight(9, 3, -1)).toBe(2);
  });
});

describe("highlightedEntry", () => {
  it("reads the row under the highlight", () => {
    expect(highlightedEntry(listing().entries, 1)?.name).toBe("beta-project");
  });

  it("has no row for nothing highlighted, or for an index past the end", () => {
    expect(highlightedEntry(listing().entries, -1)).toBeNull();
    expect(highlightedEntry(listing().entries, 3)).toBeNull();
    expect(highlightedEntry([], 0)).toBeNull();
  });
});

describe("the choice", () => {
  it("names the folder on screen, with the server's own path", () => {
    expect(choiceForListing(listing())).toBe("/home/x");
  });

  it("chooses nothing for a folder it could not read", () => {
    expect(choiceForListing(listing({ error: "not a directory" }))).toBeNull();
    expect(choiceForListing(listing({ path: "" }))).toBeNull();
  });

  it("finds a freshly created folder in the listing mkdir returned", () => {
    const after = listing({
      entries: [{ name: "gamma", path: "/home/x/gamma", isDir: true }],
    });
    expect(choiceForCreated(after, "gamma")).toBe("/home/x/gamma");
  });

  it("never invents a path for a folder the listing does not show", () => {
    expect(choiceForCreated(listing(), "delta")).toBeNull();
  });

  it("puts the highlight on a created folder's row", () => {
    const after = listing({
      entries: [
        { name: "alpha", path: "/home/x/alpha", isDir: true },
        { name: "gamma", path: "/home/x/gamma", isDir: true },
      ],
    });
    expect(indexOfEntry(after.entries, "gamma")).toBe(1);
    expect(indexOfEntry(after.entries, "nope")).toBe(-1);
  });
});