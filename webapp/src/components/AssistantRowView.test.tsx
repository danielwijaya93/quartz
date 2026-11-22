/**
 * The toggle's two halves, tested at the row that owns the decision:
 *
 *   Rich UI on  + openui-lang -> a surface
 *   Rich UI off + openui-lang -> markdown, i.e. the same text, not a broken box
 *   Rich UI on  + prose       -> markdown, because there is nothing to draw
 *
 * Plus the form round-trip: a submit reaches `onPrompt`, and typing in a field
 * reaches `onFormState` so the value survives a re-render.
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { AssistantRowView } from "./rows/AssistantRowView";
import type { AssistantRow } from "@/lib/transcript";
import { OPENUI_TURN_LANG } from "../../mock/script.mjs";

function row(text: string): AssistantRow {
  return { kind: "assistant", id: "a1", text, formState: null };
}

function renderRow(options: {
  text: string;
  richUi: boolean;
  streaming?: boolean;
  formState?: Record<string, unknown> | null;
  onPrompt?: (text: string) => void;
  onFormState?: (id: string, state: Record<string, unknown>) => void;
}) {
  return render(
    <AssistantRowView
      row={{ ...row(options.text), formState: options.formState ?? null }}
      streaming={options.streaming ?? false}
      richUi={options.richUi}
      onPrompt={options.onPrompt ?? (() => {})}
      onFormState={options.onFormState ?? (() => {})}
    />,
  );
}

describe("AssistantRowView", () => {
  it("draws openui-lang as a surface when Rich UI is on", async () => {
    renderRow({ text: OPENUI_TURN_LANG, richUi: true });

    // The renderer is a lazy chunk; the first test in this file pays for it.
    await waitFor(
      () => expect(screen.getByText("Kernel activity, last 7 days")).toBeInTheDocument(),
      { timeout: 10_000 },
    );
    expect(screen.getByTestId("openui-surface")).toBeInTheDocument();
  });

  it("keeps completed openui-lang replies rendered as a surface even when Rich UI is switched off", async () => {
    renderRow({ text: OPENUI_TURN_LANG, richUi: false });

    await waitFor(
      () => expect(screen.getByText("Kernel activity, last 7 days")).toBeInTheDocument(),
      { timeout: 10_000 },
    );
    expect(screen.getByTestId("openui-surface")).toBeInTheDocument();

    // Per-row toggle lets the user inspect raw OpenUI Lang without affecting other turns.
    fireEvent.click(screen.getByRole("button", { name: "View source" }));
    expect(screen.queryByTestId("openui-surface")).toBeNull();
    expect(screen.getByRole("button", { name: "View UI" })).toBeInTheDocument();
  });

  it("keeps prose as markdown even with Rich UI on", async () => {
    const { container } = renderRow({ text: "**hi** there", richUi: true });
    await waitFor(() => expect(container.querySelector("strong")).not.toBeNull());
    expect(screen.queryByTestId("openui-surface")).toBeNull();
  });

  it("does not treat a python code block containing root = ... as openui-lang", async () => {
    const python = "```python\nroot = TreeNode(1)\nprint(root)\n```";
    const { container } = renderRow({ text: python, richUi: true });
    await waitFor(() => expect(container.querySelector("code")).not.toBeNull());
    expect(screen.queryByTestId("openui-surface")).toBeNull();
    expect(screen.queryByRole("button", { name: "View source" })).toBeNull();
  });

  it("does not treat markdown prose with an embedded root = Stack(...) fence as openui-lang", async () => {
    const prose = "Here is an example:\n\n```python\nroot = Stack([1, 2])\n```";
    const { container } = renderRow({ text: prose, richUi: true });
    await waitFor(() => expect(container.querySelector("code")).not.toBeNull());
    expect(screen.queryByTestId("openui-surface")).toBeNull();
  });

  it("does not treat a plain variable assignment root = '/tmp' as openui-lang", async () => {
    const { container } = renderRow({ text: "root = \"/tmp/data\"", richUi: true });
    await waitFor(() => expect(container.querySelector("p")).not.toBeNull());
    expect(screen.queryByTestId("openui-surface")).toBeNull();
  });

  it("renders a fenced program while it is still streaming", async () => {
    // The fence has no closing backticks yet: detection runs on the stripped text,
    // so a partial tree renders instead of a code block full of DSL.
    const streaming = "```openui-lang\n" + OPENUI_TURN_LANG.split("\n").slice(0, 5).join("\n");
    renderRow({ text: streaming, richUi: true, streaming: true });

    await waitFor(() =>
      expect(screen.getByText("Kernel activity, last 7 days")).toBeInTheDocument(),
    );
  });

  it("sends a form submit back through the prompt path", async () => {
    const onPrompt = vi.fn();
    renderRow({ text: OPENUI_TURN_LANG, richUi: true, onPrompt });

    const submit = await screen.findByText("Apply and run");
    fireEvent.click(submit);

    await waitFor(() => expect(onPrompt).toHaveBeenCalled());
    expect(String(onPrompt.mock.calls[0]?.[0])).toContain("Apply these settings");
  });

  it("reports form state so a re-render cannot reset a half-filled form", async () => {
    const onFormState = vi.fn();
    renderRow({ text: OPENUI_TURN_LANG, richUi: true, onFormState });

    const textarea = await screen.findByPlaceholderText("Anything else Quartz should know?");
    fireEvent.change(textarea, { target: { value: "keep my context @ 40%" } });

    await waitFor(() => expect(onFormState).toHaveBeenCalled());
    const [id, state] = onFormState.mock.calls[0] ?? [];
    expect(id).toBe("a1");
    expect(JSON.stringify(state)).toContain("keep my context @ 40%");
  });
});