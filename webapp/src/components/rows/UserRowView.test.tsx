import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { UserRowView } from "./UserRowView";
import type { UserRow } from "@/lib/transcript";

function makeRow(text: string, images: UserRow["images"] = []): UserRow {
  return { kind: "user", id: "u1", text, images };
}

describe("UserRowView", () => {
  it("renders plain user text without attachment cards", () => {
    render(<UserRowView row={makeRow("Hello Quartz")} />);
    expect(screen.getByText("Hello Quartz")).toBeInTheDocument();
  });

  it("renders a compact card for an attached CSV instead of printing its raw content", () => {
    const csv = "Ticker,Price\nSPY,500\nQQQ,450";
    const text = `Run stock market recap\n\n[Attached file: 2026-11-25 post.csv]\n\`\`\`\n${csv}\n\`\`\``;

    render(<UserRowView row={makeRow(text)} />);

    expect(screen.getByText("Run stock market recap")).toBeInTheDocument();
    expect(screen.getByText("2026-11-25 post.csv")).toBeInTheDocument();
    expect(screen.getByText(/3 lines/)).toBeInTheDocument();
    expect(screen.queryByText(/Ticker,Price/)).not.toBeInTheDocument();
    expect(screen.queryByText(/SPY,500/)).not.toBeInTheDocument();
  });

  it("renders multiple attached file cards when multiple files are attached", () => {
    const text = [
      "Compare these two files",
      "[Attached file: a.csv]\n```\ncol1,col2\n1,2\n```",
      "[Attached file: notes.txt]\n```\nsome notes\n```",
    ].join("\n\n");

    render(<UserRowView row={makeRow(text)} />);

    expect(screen.getByText("Compare these two files")).toBeInTheDocument();
    expect(screen.getByText("a.csv")).toBeInTheDocument();
    expect(screen.getByText(/2 lines/)).toBeInTheDocument();
    expect(screen.getByText("notes.txt")).toBeInTheDocument();
    expect(screen.getByText(/1 line(?!s)/)).toBeInTheDocument();
    expect(screen.queryByText(/col1,col2/)).not.toBeInTheDocument();
    expect(screen.queryByText(/some notes/)).not.toBeInTheDocument();
  });

  it("renders attachment card even when user sends no accompanying text", () => {
    const text = "[Attached file: data.csv]\n```\na,b\n```";

    render(<UserRowView row={makeRow(text)} />);

    expect(screen.getByText("data.csv")).toBeInTheDocument();
    expect(screen.queryByText(/a,b/)).not.toBeInTheDocument();
  });

  it("does not treat plain prose mentioning [Attached file: ...] without a code fence as a card", () => {
    const text = "Please check [Attached file: foo.csv] later";

    render(<UserRowView row={makeRow(text)} />);

    expect(screen.getByText(text)).toBeInTheDocument();
  });

  it("renders only plain text for OpenUI reply action messages", () => {
    const text = [
      "]]>openui:content",
      "5 minutes passed and my Cron Test Bot didn't run. Help me check why.",
      "]]>openui:context",
      '["User clicked: 5 minutes passed and my Cron Test Bot didn\'t run. Help me check why.",{}]',
    ].join("\n");

    render(<UserRowView row={makeRow(text)} />);

    expect(
      screen.getByText("5 minutes passed and my Cron Test Bot didn't run. Help me check why."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/openui:content/)).not.toBeInTheDocument();
    expect(screen.queryByText(/openui:context/)).not.toBeInTheDocument();
    expect(screen.queryByText(/User clicked:/)).not.toBeInTheDocument();
  });

  it("strips context-only OpenUI action messages without leaking protocol markers", () => {
    const text = [
      "]]>openui:context",
      '["User clicked: ",{"effort":"high"}]',
    ].join("\n");

    const { container } = render(<UserRowView row={makeRow(text)} />);

    expect(container.textContent).not.toContain("openui:context");
    expect(container.textContent).not.toContain("User clicked:");
  });
});
