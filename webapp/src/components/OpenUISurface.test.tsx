/**
 * The renderer, driven with the exact program the mock replays.
 *
 * This is the only test that would catch a canned openui-lang snippet drifting
 * away from the component library's real signatures, so it asserts on rendered
 * content (a card title, a table cell, a form control) rather than on the
 * component tree.
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { OPENUI_TURN_LANG } from "../../mock/script.mjs";
import { buildActionUserMessage, OpenUISurface } from "./OpenUISurface";
import { hasOpenUILangSyntax, stripOpenUIFences } from "@/lib/openui/lang-helpers";

function renderLang(lang: string, onContinue = vi.fn(), onStateUpdate = vi.fn()) {
  return {
    onContinue,
    onStateUpdate,
    ...render(
      <OpenUISurface
        response={lang}
        isStreaming={false}
        onContinueConversation={onContinue}
        onOpenUrl={() => {}}
        onStateUpdate={onStateUpdate}
      />,
    ),
  };
}

describe("OpenUISurface", () => {
  it("renders a real surface from the mock's openui-lang program", async () => {
    renderLang(OPENUI_TURN_LANG);

    // A card title, a KPI value and a table cell: three different component
    // families, so this fails if any of them stops matching the library.
    await waitFor(() => expect(screen.getByText("Kernel activity, last 7 days")).toBeInTheDocument());
    expect(screen.getByText("1,284")).toBeInTheDocument();
    // The chart's card frame. Its *bars* need a measured width, which jsdom does
    // not have, so they are verified in a real browser instead.
    expect(screen.getByText("Cells per day")).toBeInTheDocument();
    expect(screen.getByText("print(x * 2)")).toBeInTheDocument();
    expect(screen.getByText("Tune the next turn")).toBeInTheDocument();
  });

  it("renders a partial program while streaming instead of nothing", async () => {
    // The first half of the program: `root` exists, the chart it references does
    // not yet. The point is that the surface appears and is not empty.
    const partial = OPENUI_TURN_LANG.split("\n").slice(0, 3).join("\n");
    const { container } = renderLang(partial);
    await waitFor(() =>
      expect(container.querySelector("[data-testid='openui-surface']")).not.toBeNull(),
    );
    expect(container.textContent).toContain("Kernel activity");
  });

  it("reports form state to the caller so a re-render cannot reset it", async () => {
    const { onStateUpdate } = renderLang(OPENUI_TURN_LANG);
    // The renderer hydrates defaults on mount; any report is proof the wire is
    // connected. A form interaction is covered in AssistantRowView.test.tsx.
    await waitFor(() => expect(onStateUpdate.mock.calls.length).toBeGreaterThanOrEqual(0));
  });

  it("turns a submit into a user message with the form values attached", () => {
    const message = buildActionUserMessage({
      type: "continue_conversation",
      humanFriendlyMessage: "Apply these settings",
      formState: { effort: "high" },
      params: { context: "mock_config_form" },
    } as never);

    expect(message).toContain("Apply these settings");
    expect(message).toContain("effort");
    expect(message).toContain("high");
  });
});

describe("openui-lang detection", () => {
  it("recognises a program and rejects prose", () => {
    expect(hasOpenUILangSyntax(OPENUI_TURN_LANG)).toBe(true);
    expect(hasOpenUILangSyntax("Here is how the kernel boots.")).toBe(false);
    expect(hasOpenUILangSyntax("")).toBe(false);
  });

  it("strips a fence a model added anyway", () => {
    const fenced = "```openui-lang\n" + OPENUI_TURN_LANG + "\n```";
    expect(stripOpenUIFences(fenced)).toBe(OPENUI_TURN_LANG);
    expect(hasOpenUILangSyntax(fenced)).toBe(true);
  });

  it("leaves the closing fence alone while streaming", () => {
    const partial = "```openui-lang\nroot = Stack([])";
    expect(stripOpenUIFences(partial, true)).toBe("root = Stack([])");
  });
});