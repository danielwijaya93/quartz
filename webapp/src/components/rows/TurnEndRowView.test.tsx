import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { TurnEndRowView } from "./TurnEndRowView";

describe("TurnEndRowView", () => {
  it("renders tok/s when completionTokens and elapsedMs are positive", () => {
    render(
      <TurnEndRowView
        row={{
          kind: "turn_end",
          id: "te1",
          status: "ok",
          tokens: 13793,
          completionTokens: 336,
          elapsedMs: 8000,
        }}
      />,
    );
    expect(screen.getByText("13,793 tokens")).toBeInTheDocument();
    expect(screen.getByText("8.0s")).toBeInTheDocument();
    expect(screen.getByText("42 tok/s")).toBeInTheDocument();
  });

  it("formats sub-10 tok/s with one decimal place", () => {
    render(
      <TurnEndRowView
        row={{
          kind: "turn_end",
          id: "te2",
          status: "ok",
          tokens: 500,
          completionTokens: 9,
          elapsedMs: 2000,
        }}
      />,
    );
    expect(screen.getByText("4.5 tok/s")).toBeInTheDocument();
  });

  it("omits tok/s when completionTokens is missing, zero, or elapsedMs is zero", () => {
    const { rerender } = render(
      <TurnEndRowView
        row={{ kind: "turn_end", id: "te3", status: "ok", tokens: 500, elapsedMs: 2000 }}
      />,
    );
    expect(screen.queryByText(/tok\/s/)).toBeNull();

    rerender(
      <TurnEndRowView
        row={{
          kind: "turn_end",
          id: "te4",
          status: "ok",
          tokens: 500,
          completionTokens: 0,
          elapsedMs: 2000,
        }}
      />,
    );
    expect(screen.queryByText(/tok\/s/)).toBeNull();

    rerender(
      <TurnEndRowView
        row={{
          kind: "turn_end",
          id: "te5",
          status: "ok",
          tokens: 500,
          completionTokens: 100,
          elapsedMs: 0,
        }}
      />,
    );
    expect(screen.queryByText(/tok\/s/)).toBeNull();
  });
});
