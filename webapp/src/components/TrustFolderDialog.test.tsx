import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { TrustFolderDialog } from "./TrustFolderDialog";

describe("TrustFolderDialog", () => {
  it("renders the folder path and detected .quartz files when open", () => {
    render(
      <TrustFolderDialog
        open={true}
        cwd="/home/user/untrusted-repo"
        files={[".quartz/helpers.py", ".quartz/mcp.json"]}
        onTrust={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );

    expect(screen.getByText("Trust this folder?")).toBeInTheDocument();
    expect(screen.getByText("/home/user/untrusted-repo")).toBeInTheDocument();
    expect(screen.getByText(".quartz/helpers.py")).toBeInTheDocument();
    expect(screen.getByText(".quartz/mcp.json")).toBeInTheDocument();
  });

  it("calls onTrust when Trust is clicked and onDismiss when Keep untrusted is clicked", () => {
    const onTrust = vi.fn();
    const onDismiss = vi.fn();

    const { unmount } = render(
      <TrustFolderDialog
        open={true}
        cwd="/home/user/repo"
        files={[".quartz/helpers.py"]}
        onTrust={onTrust}
        onDismiss={onDismiss}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Yes, trust this folder" }));
    expect(onTrust).toHaveBeenCalledTimes(1);
    unmount();

    render(
      <TrustFolderDialog
        open={true}
        cwd="/home/user/repo"
        files={[".quartz/helpers.py"]}
        onTrust={onTrust}
        onDismiss={onDismiss}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Keep untrusted for now" }));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("toggles focus between the two choices on ArrowDown/ArrowUp", () => {
    render(
      <TrustFolderDialog
        open={true}
        cwd="/home/user/repo"
        files={[".quartz/mcp.json"]}
        onTrust={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );

    const dismissBtn = screen.getByRole("button", { name: "Keep untrusted for now" });
    const trustBtn = screen.getByRole("button", { name: "Yes, trust this folder" });

    dismissBtn.focus();
    expect(document.activeElement).toBe(dismissBtn);

    fireEvent.keyDown(dismissBtn.closest("form")!, { key: "ArrowDown" });
    expect(document.activeElement).toBe(trustBtn);

    fireEvent.keyDown(trustBtn.closest("form")!, { key: "ArrowUp" });
    expect(document.activeElement).toBe(dismissBtn);
  });
});
