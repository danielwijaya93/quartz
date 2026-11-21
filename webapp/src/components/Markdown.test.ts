import { describe, expect, it } from "vitest";
import { closeUnterminatedFence } from "./Markdown";

describe("closeUnterminatedFence", () => {
  it("leaves balanced fences alone", () => {
    const markdown = "```python\nprint(1)\n```\n";
    expect(closeUnterminatedFence(markdown)).toBe(markdown);
  });

  it("leaves fence-free prose alone", () => {
    expect(closeUnterminatedFence("just prose")).toBe("just prose");
  });

  it("closes a single open fence", () => {
    const closed = closeUnterminatedFence("```python\nprint(1)");
    expect(closed).toBe("```python\nprint(1)\n```\n");
  });

  it("closes an open fence that already ends with a newline", () => {
    const closed = closeUnterminatedFence("```python\nprint(1)\n");
    expect(closed).toBe("```python\nprint(1)\n```\n");
  });

  it("does not close a balanced pair in the middle of a longer stream", () => {
    const markdown = "```\na\n```\n\nprose\n\n```\nb\n```\n";
    expect(closeUnterminatedFence(markdown)).toBe(markdown);
  });
});
