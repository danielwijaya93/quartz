import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { McpView } from "./McpView";

describe("McpView", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("lists MCP servers, adds, toggles, removes, and returns via Back to chat", async () => {
    const user = userEvent.setup();
    const onBack = vi.fn();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/api/mcp") {
        return new Response(JSON.stringify({ configPath: "~/.quartz/mcp.json", servers: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url === "/api/mcp/add") {
        return new Response(
          JSON.stringify({
            ok: true,
            configPath: "~/.quartz/mcp.json",
            servers: [
              {
                name: "fs",
                enabled: true,
                transport: "stdio",
                url: "",
                command: "npx",
                args: ["-y", "server"],
                cwd: "",
                headerKeys: [],
                envKeys: [],
              },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url === "/api/mcp/toggle") {
        return new Response(
          JSON.stringify({
            ok: true,
            configPath: "~/.quartz/mcp.json",
            servers: [
              {
                name: "fs",
                enabled: false,
                transport: "stdio",
                url: "",
                command: "npx",
                args: ["-y", "server"],
                cwd: "",
                headerKeys: [],
                envKeys: [],
              },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response(
        JSON.stringify({ ok: true, configPath: "~/.quartz/mcp.json", servers: [] }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    });

    render(<McpView onBack={onBack} />);
    expect(await screen.findByText("No MCP servers configured yet.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Add MCP Server" }));
    await user.type(screen.getByLabelText("MCP server name"), "fs");
    await user.type(screen.getByLabelText("MCP server command"), "npx -y server");
    await user.click(screen.getByRole("button", { name: "Save Server" }));

    expect(await screen.findByText("fs")).toBeInTheDocument();
    await user.click(screen.getByRole("switch", { name: "Toggle MCP server fs" }));
    expect(screen.getByRole("switch", { name: "Toggle MCP server fs" })).toHaveAttribute(
      "aria-checked",
      "false",
    );

    await user.click(screen.getByRole("button", { name: "Remove MCP server fs" }));
    expect(await screen.findByText("No MCP servers configured yet.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Back to chat" }));
    expect(onBack).toHaveBeenCalledTimes(1);
  });
});
