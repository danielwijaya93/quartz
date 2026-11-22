import { describe, expect, it } from "vitest";
import {
  currentName,
  filterDirectories,
  parentPath,
  parseListing,
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
