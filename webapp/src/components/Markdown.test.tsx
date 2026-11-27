import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Markdown, closeUnterminatedFence, splitMarkdownBlocks } from "./Markdown";

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

describe("splitMarkdownBlocks", () => {
  it("splits paragraphs on blank lines while keeping fenced code blocks intact", () => {
    const md = "First paragraph.\n\n```python\na = 1\n\nb = 2\n```\n\nSecond paragraph.";
    expect(splitMarkdownBlocks(md)).toEqual([
      "First paragraph.",
      "```python\na = 1\n\nb = 2\n```",
      "Second paragraph.",
    ]);
  });

  it("handles empty string and single block", () => {
    expect(splitMarkdownBlocks("")).toEqual([]);
    expect(splitMarkdownBlocks("hello world")).toEqual(["hello world"]);
  });
});

/** A fenced block, the shape every agent reply contains sooner or later. */
const CODE = "```python\ndef mean(xs):\n    return sum(xs) / len(xs)\n```";

/** The Clipboard API, as jsdom does not provide one. */
function stubClipboard(writeText: (text: string) => Promise<void>): void {
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
}

describe("code block copy", () => {
  afterEach(() => {
    // Leave no clipboard behind for the next test to inherit.
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    vi.restoreAllMocks();
  });

  it("copies the code, not the highlighted markup", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn<(text: string) => Promise<void>>().mockResolvedValue(undefined);
    stubClipboard(writeText);
    render(<Markdown>{CODE}</Markdown>);

    // The block is highlighted...
    const block = document.querySelector("pre");
    expect(block?.querySelector(".hljs-keyword, .hljs-keyword, span")).not.toBeNull();

    await user.click(screen.getByRole("button", { name: /Copy code/ }));

    // ...and what lands on the clipboard is the source, with no spans and no
    // trailing fence newline.
    expect(writeText).toHaveBeenCalledWith("def mean(xs):\n    return sum(xs) / len(xs)");
  });

  it("confirms the copy in words, for the eye and for a screen reader", async () => {
    const user = userEvent.setup();
    stubClipboard(() => Promise.resolve());
    render(<Markdown>{CODE}</Markdown>);

    await user.click(screen.getByRole("button", { name: /Copy code/ }));

    // The button says so (its visible text and so its accessible name)...
    expect(screen.getByRole("button", { name: /Copied code/ })).toBeInTheDocument();
    // ...and a polite live region says it for a screen reader, which announces
    // neither a name change nor a colour.
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Copied to clipboard."));
  });

  it("is reachable from the keyboard", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn<(text: string) => Promise<void>>().mockResolvedValue(undefined);
    stubClipboard(writeText);
    render(<Markdown>{CODE}</Markdown>);

    await user.tab();
    expect(screen.getByTestId("copy-code")).toHaveFocus();
    await user.keyboard("{Enter}");

    expect(writeText).toHaveBeenCalledOnce();
  });

  it("fails quietly when there is no Clipboard API", async () => {
    // Deliberately no `userEvent.setup()`: it installs a clipboard stub of its own.
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    render(<Markdown>{CODE}</Markdown>);
    expect(navigator.clipboard).toBeUndefined();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Copy code/ }));
    });

    // No throw, no error row, and the button still offers the copy.
    expect(screen.getByRole("button", { name: /Copy code/ })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("");
  });

  it("fails quietly when the clipboard refuses", async () => {
    const user = userEvent.setup();
    stubClipboard(() => Promise.reject(new Error("denied")));
    render(<Markdown>{CODE}</Markdown>);

    await user.click(screen.getByRole("button", { name: /Copy code/ }));

    expect(screen.getByRole("button", { name: /Copy code/ })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("");
  });

  it("leaves inline code alone", () => {
    render(<Markdown>{`run \`print(1)\` first`}</Markdown>);
    expect(screen.queryByRole("button", { name: /Copy/ })).toBeNull();
  });
});
