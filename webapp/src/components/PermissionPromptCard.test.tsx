import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
    expect(screen.getByText("git remote")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Allow once" }));
    expect(onRespond).toHaveBeenCalledWith("req-1", "allow_once");

    fireEvent.click(screen.getByRole("button", { name: "Allow git remote for session" }));
    expect(onRespond).toHaveBeenCalledWith("req-1", "allow_session");

    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(onRespond).toHaveBeenCalledWith("req-1", "reject");
  });

  it("uses shrink-0 opaque container, full-width scrollable command box, and word wrapping", () => {
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
    expect(region.className).toContain("shrink-0");
    const card = region.firstElementChild;
    expect(card?.className).toContain("bg-surface-secondary");
    expect(card?.className).not.toContain("bg-surface-secondary/90");

    const code = screen.getByText(/gh pr create/);
    expect(code.className).toContain("whitespace-pre-wrap");
    expect(code.className).toContain("break-words");
    expect(code.className).not.toContain("truncate");
  });

  it("supports expanding and collapsing long commands", () => {
    const longCommand = Array.from({ length: 12 }, (_, i) => `line-${i + 1}: --flag value`).join("\n");
    render(
      <PermissionPromptCard
        permission={{
          requestId: "req-3",
          tool: "gh",
          command: longCommand,
          cwd: "/tmp/repo",
          scope: "gh",
        }}
        onRespond={vi.fn()}
      />,
    );

    const toggle = screen.getByRole("button", { name: /Expand command/i });
    const scrollBox = screen.getByTestId("permission-command-scroll");
    expect(scrollBox.className).toContain("max-h-36");

    fireEvent.click(toggle);
    expect(screen.getByRole("button", { name: /Collapse command/i })).toBeInTheDocument();
    expect(scrollBox.className).toContain("max-h-[50vh]");

    fireEvent.click(screen.getByRole("button", { name: /Collapse command/i }));
    expect(scrollBox.className).toContain("max-h-36");
  });

  it("copies the command to clipboard", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });

    render(
      <PermissionPromptCard
        permission={{
          requestId: "req-4",
          tool: "git",
          command: "git status",
          cwd: "/tmp/repo",
          scope: "git",
        }}
        onRespond={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Copy command" }));
    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith("git status");
    });
    expect(screen.getByRole("button", { name: "Copied command" })).toBeInTheDocument();
  });
});
