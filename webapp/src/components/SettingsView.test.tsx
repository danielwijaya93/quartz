import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SettingsView } from "./SettingsView";

describe("SettingsView", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders in-canvas settings without a modal dialog and returns via Back to chat", async () => {
    const user = userEvent.setup();
    const onBack = vi.fn();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/api/mcp") {
        return new Response(
          JSON.stringify({
            configPath: "~/.quartz/mcp.json",
            servers: [
              {
                name: "exa",
                enabled: true,
                transport: "http",
                url: "https://mcp.exa.ai/mcp",
                command: "",
                args: [],
                cwd: "",
                headerKeys: ["Authorization"],
                envKeys: [],
              },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response(
        JSON.stringify({
          values: { reserve: 16384, keep: 32768, progress: 5, bg: 30 },
          defaults: { reserve: 16384, keep: 32768, progress: 5, bg: 30 },
          configPath: "~/.quartz/config.json",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    });

    render(<SettingsView onBack={onBack} />);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(await screen.findByLabelText("Reserve tokens")).toHaveValue("16384");
    expect(screen.getByText("Stored in ~/.quartz/config.json")).toBeInTheDocument();
    expect(await screen.findByText("exa")).toBeInTheDocument();
    expect(screen.getByText("headers: Authorization")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Back to chat" }));
    expect(onBack).toHaveBeenCalledTimes(1);
  });

  it("supports toggling, adding, and removing MCP servers", async () => {
    const user = userEvent.setup();
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
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
      if (url === "/api/mcp/remove") {
        return new Response(
          JSON.stringify({ ok: true, configPath: "~/.quartz/mcp.json", servers: [] }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response(
        JSON.stringify({
          values: { reserve: 16384, keep: 32768, progress: 5, bg: 30 },
          defaults: { reserve: 16384, keep: 32768, progress: 5, bg: 30 },
          configPath: "~/.quartz/config.json",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    });

    render(<SettingsView onBack={vi.fn()} />);
    expect(await screen.findByText("No MCP servers configured yet.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Add MCP Server" }));
    await user.type(screen.getByLabelText("MCP server name"), "fs");
    await user.type(screen.getByLabelText("MCP server command"), "npx -y server");
    await user.click(screen.getByRole("button", { name: "Save Server" }));

    expect(await screen.findByText("fs")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Remove MCP server fs" }));
    expect(await screen.findByText("No MCP servers configured yet.")).toBeInTheDocument();
    fetchSpy.mockRestore();
  });
});
