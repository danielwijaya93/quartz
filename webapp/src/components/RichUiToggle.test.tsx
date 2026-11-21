import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RichUiToggle } from "./RichUiToggle";

describe("RichUiToggle", () => {
  it("shows both states with text, not colour alone", () => {
    render(<RichUiToggle mode="markdown" onChange={() => {}} />);
    expect(screen.getByRole("radiogroup", { name: "Response format" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Markdown response" })).toHaveTextContent("Markdown");
    expect(screen.getByRole("radio", { name: "Dashboard response" })).toHaveTextContent("Dashboard");
  });

  it("announces the active state", () => {
    render(<RichUiToggle mode="markdown" onChange={() => {}} />);
    expect(screen.getByRole("radio", { name: "Markdown response" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByRole("radio", { name: "Dashboard response" })).toHaveAttribute(
      "aria-checked",
      "false",
    );
  });

  it("reports the chosen state", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<RichUiToggle mode="markdown" onChange={onChange} />);

    await user.click(screen.getByRole("radio", { name: "Dashboard response" }));
    expect(onChange).toHaveBeenCalledWith("dashboard");
  });
});
