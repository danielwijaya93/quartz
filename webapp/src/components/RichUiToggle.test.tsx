import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RichUiToggle } from "./RichUiToggle";

describe("RichUiToggle", () => {
  it("renders a labelled Rich UI switch that is off in markdown mode", () => {
    render(<RichUiToggle mode="markdown" onChange={() => {}} />);

    const toggle = screen.getByRole("switch", { name: "Rich UI" });
    expect(toggle).toBeInTheDocument();
    expect(toggle).not.toBeChecked();
    expect(screen.getByText("Rich UI")).toBeInTheDocument();
  });

  it("marks the switch as checked when Rich UI is on", () => {
    render(<RichUiToggle mode="rich" onChange={() => {}} />);
    expect(screen.getByRole("switch", { name: "Rich UI" })).toBeChecked();
  });

  it("toggles between markdown and rich", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(<RichUiToggle mode="markdown" onChange={onChange} />);

    await user.click(screen.getByRole("switch", { name: "Rich UI" }));
    expect(onChange).toHaveBeenCalledWith("rich");

    rerender(<RichUiToggle mode="rich" onChange={onChange} />);
    await user.click(screen.getByRole("switch", { name: "Rich UI" }));
    expect(onChange).toHaveBeenCalledWith("markdown");
  });
});
