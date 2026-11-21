import { StrictMode } from "react";
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { A2uiRowView } from "./rows/A2uiRowView";
import { cannedSurface, SURFACE_ID } from "../../mock/a2ui-fixture.mjs";
import type { A2uiRow } from "@/lib/transcript";

/**
 * The paint check, in test form.
 *
 * These assert on text the *catalog* produced ("Quartz dashboard", the caption),
 * not on a class name or a wrapper: if the surface renders, the basic catalog's
 * `Text` and `Column` really ran, which is the thing this PR exists to prove.
 */
function row(messages: Record<string, unknown>[] = cannedSurface()): A2uiRow {
  return { kind: "a2ui", id: "a2ui-turn-1-quartz-dashboard", surfaceId: SURFACE_ID, messages };
}

describe("A2uiRowView", () => {
  it("renders a surface from the known-good fixture", async () => {
    render(<A2uiRowView row={row()} />);

    expect(screen.getByRole("region", { name: "Dashboard" })).toBeInTheDocument();
    // Markdown from the basic catalog's Text, rendered by the app's renderer.
    expect(await screen.findByText("Quartz dashboard")).toBeInTheDocument();
    expect(await screen.findByText(/basic catalog/)).toBeInTheDocument();
    // The fallback must not be what is on screen.
    expect(screen.queryByText("No dashboard was drawn")).not.toBeInTheDocument();
  });

  it("explains a broken payload instead of leaving a blank area", () => {
    render(
      <A2uiRowView
        row={row([{ version: "1.0", updateComponents: { surfaceId: SURFACE_ID, components: [] } }])}
      />,
    );

    expect(screen.getByText("No dashboard was drawn")).toBeInTheDocument();
    // The renderer's verdict, not ours: a rejected message is why nothing drew.
    expect(screen.getByText(/at least 1 element/)).toBeInTheDocument();
  });

  it("reports a rejected envelope rather than throwing", () => {
    render(
      <A2uiRowView
        row={row([
          { version: "1.0", createSurface: { surfaceId: SURFACE_ID } },
          {
            version: "1.0",
            updateComponents: {
              surfaceId: SURFACE_ID,
              components: [{ id: "not-a-valid-id", component: "Text", text: "hi" }],
            },
          },
        ])}
      />,
    );

    expect(screen.getByText("No dashboard was drawn")).toBeInTheDocument();
    // The renderer's own words: names the offending id, which is the fix.
    expect(screen.getByText(/not-a-valid-id/)).toBeInTheDocument();
  });

  it("renders the same surface after a StrictMode remount, and releases it on unmount", async () => {
    const { unmount } = render(
      <StrictMode>
        <A2uiRowView row={row()} />
      </StrictMode>,
    );
    expect(await screen.findAllByText("Quartz dashboard")).not.toHaveLength(0);

    // Disposal is deferred by a microtask; flushing it proves the unmount path
    // runs without throwing and without blanking anything it already drew.
    unmount();
    await Promise.resolve();
    expect(document.querySelector(".a2ui-scope")).toBeNull();
  });

  it("says so when the frame carried no messages at all", () => {
    render(<A2uiRowView row={row([])} />);
    expect(screen.getByText(/sent no messages/)).toBeInTheDocument();
  });
});
