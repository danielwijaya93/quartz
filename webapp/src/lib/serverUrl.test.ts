import { afterEach, describe, expect, it } from "vitest";
import {
  apiUrl,
  clearServerUrl,
  configuredWsUrl,
  getServerUrl,
  normalizeServerUrl,
  setServerUrl,
} from "./serverUrl";

afterEach(() => {
  clearServerUrl();
});

describe("normalizeServerUrl", () => {
  it("adds http:// when the scheme is missing", () => {
    expect(normalizeServerUrl("100.64.0.1:8765")).toBe("http://100.64.0.1:8765");
  });

  it("keeps https and strips a trailing slash", () => {
    expect(normalizeServerUrl("https://quartz.example.test/")).toBe(
      "https://quartz.example.test",
    );
  });

  it("rejects a non-http scheme", () => {
    expect(() => normalizeServerUrl("ftp://host")).toThrow(/http/);
  });

  it("returns an empty string for blank input", () => {
    expect(normalizeServerUrl("   ")).toBe("");
  });
});

describe("server url storage", () => {
  it("round-trips a normalized url", () => {
    expect(getServerUrl()).toBeNull();
    setServerUrl("100.64.0.1:8765");
    expect(getServerUrl()).toBe("http://100.64.0.1:8765");
  });

  it("forgets the url on clear", () => {
    setServerUrl("http://host:1234");
    clearServerUrl();
    expect(getServerUrl()).toBeNull();
  });
});

describe("derived endpoints", () => {
  it("prefixes api paths with the configured base", () => {
    setServerUrl("http://100.64.0.1:8765");
    expect(apiUrl("/api/health")).toBe("http://100.64.0.1:8765/api/health");
    expect(apiUrl("/api/jobs")).toBe("http://100.64.0.1:8765/api/jobs");
  });

  it("passes api paths through when no server is set", () => {
    expect(apiUrl("/api/health")).toBe("/api/health");
  });

  it("derives a ws url from the http base", () => {
    setServerUrl("http://100.64.0.1:8765");
    expect(configuredWsUrl()).toBe("ws://100.64.0.1:8765/ws");
  });

  it("derives a wss url from an https base", () => {
    setServerUrl("https://quartz.example.test");
    expect(configuredWsUrl()).toBe("wss://quartz.example.test/ws");
  });

  it("has no ws url without a configured server", () => {
    expect(configuredWsUrl()).toBeNull();
  });
});
