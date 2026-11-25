import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { PermissionPromptCard } from "./PermissionPromptCard";

describe("PermissionPromptCard", () => {
  it("renders command and dispatches allow_once, allow_session, and reject", () => {
    const onRespond = vi.fn();
    render(
      <PermissionPromptCard
        permission={{
          requestId: "req-1",
          tool: "git",
          command: "git push -u origin main",
          cwd: "/tmp/repo",
          scope: "git_remote",
        }}
        onRespond={onRespond}
      />,
    );

    expect(screen.getByText("git push -u origin main")).toBeInTheDocument();
    expect(screen.getByText("in /tmp/repo")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Allow once" }));
    expect(onRespond).toHaveBeenCalledWith("req-1", "allow_once");

    fireEvent.click(screen.getByRole("button", { name: "Allow git remote for session" }));
    expect(onRespond).toHaveBeenCalledWith("req-1", "allow_session");

    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(onRespond).toHaveBeenCalledWith("req-1", "reject");
  });

  it("stacks content above action buttons on mobile and wraps long commands", () => {
    render(
      <PermissionPromptCard
        permission={{
          requestId: "req-2",
          tool: "gh",
          command: "gh pr create --title 'Fix mobile layout' --body 'Details'",
          cwd: "/tmp/repo",
          scope: "gh",
        }}
        onRespond={vi.fn()}
      />,
    );

    const region = screen.getByRole("region", { name: "Permission request" });
    const layoutRow = region.querySelector(".flex-col");
    expect(layoutRow).not.toBeNull();
    expect(layoutRow?.className).toContain("sm:flex-row");

    const code = screen.getByText(/gh pr create/);
    expect(code.className).toContain("break-all");
    expect(code.className).not.toContain("truncate");
  });
});
