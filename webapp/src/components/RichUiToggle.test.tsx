import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RichUiToggle } from "./RichUiToggle";

describe("RichUiToggle", () => {
  it("offers both modes as labelled options, so nothing is colour-only", () => {
    render(<RichUiToggle mode="markdown" onChange={() => {}} />);

    expect(screen.getByRole("radiogroup", { name: "Assistant reply format" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Markdown replies/ })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Rich UI replies/ })).toBeInTheDocument();
    // The visible text is the label, not just the icon: a reader who cannot see
    // the selected fill still knows which option is which.
    expect(screen.getByText("Markdown")).toBeInTheDocument();
    expect(screen.getByText("Rich UI")).toBeInTheDocument();
  });

  it("marks the current mode as checked", () => {
    render(<RichUiToggle mode="rich" onChange={() => {}} />);
    expect(screen.getByRole("radio", { name: /Rich UI replies/ })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByRole("radio", { name: /Markdown replies/ })).toHaveAttribute(
      "aria-checked",
      "false",
    );
  });

  it("reports the chosen mode", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<RichUiToggle mode="markdown" onChange={onChange} />);

    await user.click(screen.getByRole("radio", { name: /Rich UI replies/ }));
    expect(onChange).toHaveBeenCalledWith("rich");
  });
});
