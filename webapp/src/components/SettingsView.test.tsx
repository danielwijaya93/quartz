import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SettingsView } from "./SettingsView";

describe("SettingsView", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders in-canvas settings without a modal dialog or MCP clutter and returns via Back to chat", async () => {
    const user = userEvent.setup();
    const onBack = vi.fn();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.endsWith("/api/health")) {
        return new Response(JSON.stringify({ status: "ok", version: "0.1.0" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
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
    expect(screen.queryByText("MCP Servers")).toBeNull();
    expect(await screen.findByLabelText("Reserve tokens")).toHaveValue("16384");
    expect(screen.getByText("Stored in ~/.quartz/config.json")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Back to chat" }));
    expect(onBack).toHaveBeenCalledTimes(1);
  });

  it("shows App notifications instead of Browser notifications inside Capacitor", async () => {
    const serverUrl = await import("../lib/serverUrl");
    vi.spyOn(serverUrl, "isNativeCapacitor").mockReturnValue(true);
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.endsWith("/api/health")) {
        return new Response(JSON.stringify({ status: "ok", version: "0.1.1" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
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
    expect(await screen.findByText("App notifications")).toBeInTheDocument();
    expect(screen.queryByText("Browser notifications")).toBeNull();
  });

  it("renders dedicated Notifications section with toggle-able categories", async () => {
    const user = userEvent.setup();
    const posted: Array<{ key: string; value: string }> = [];
    const serverUrl = await import("../lib/serverUrl");
    vi.spyOn(serverUrl, "isNativeCapacitor").mockReturnValue(true);
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.endsWith("/api/health")) {
        return new Response(JSON.stringify({ status: "ok", version: "0.1.1" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.endsWith("/api/settings") && init?.method === "POST") {
        posted.push(JSON.parse(String(init.body)) as { key: string; value: string });
        return new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
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
    expect(await screen.findByRole("region", { name: "Notifications" })).toBeInTheDocument();

    const bgSwitch = screen.getByRole("switch", { name: "Background jobs" });
    expect(bgSwitch).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("switch", { name: "Turn updates" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("switch", { name: "Approval requests" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("switch", { name: "Errors" })).toHaveAttribute("aria-checked", "true");

    await user.click(bgSwitch);
    expect(bgSwitch).toHaveAttribute("aria-checked", "false");
    expect(posted).toContainEqual({ key: "notifyBackground", value: "off" });
  });

  it("renders kernel quotas, security controls, bot governance defaults, and live diagnostics", async () => {
    const user = userEvent.setup();
    const posted: Array<{ key?: string; value?: string }> = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.endsWith("/api/health")) {
        return new Response(JSON.stringify({ status: "ok", version: "0.21.0" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.endsWith("/api/metrics")) {
        return new Response(
          JSON.stringify({
            db: { healthy: true, schemaVersion: 2 },
            sessions: { active: 2 },
            usage24h: { events: 4, inputTokens: 12000, outputTokens: 3000, costUsd: 0.1234 },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url.includes("/api/audit")) {
        return new Response(
          JSON.stringify({
            events: [
              {
                id: 1,
                ts: 1750000000,
                eventType: "bridge_rejected",
                actor: "sandbox",
                decision: "rejected",
                summary: "blocked git -c",
              },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (init?.method === "POST") {
        const body = JSON.parse(String(init.body ?? "{}")) as { key?: string; value?: string };
        posted.push(body);
        return new Response(
          JSON.stringify({
            ok: true,
            message: "Saved.",
            values: {
              reserve: 16384,
              keep: 32000,
              progress: 5,
              bg: 300,
              idle: 2,
              kernelMemMb: 4096,
              kernelMaxOpenFiles: 4096,
              kernelMaxProcesses: 256,
              strictSnapshots: body.key === "strict_snapshots" ? body.value === "on" : true,
              sandboxEnvPassthrough: body.key === "sandbox_env" ? body.value : "",
              botMaxUsdPerDay: null,
              botMaxConsecutiveErrors: 5,
              botWebhookRateLimit: 30,
            },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response(
        JSON.stringify({
          values: {
            reserve: 16384,
            keep: 32000,
            progress: 5,
            bg: 300,
            idle: 2,
            kernelMemMb: 4096,
            kernelMaxOpenFiles: 4096,
            kernelMaxProcesses: 256,
            strictSnapshots: true,
            sandboxEnvPassthrough: "MY_APP_ENV",
            botMaxUsdPerDay: 5,
            botMaxConsecutiveErrors: 5,
            botWebhookRateLimit: 30,
          },
          defaults: {
            reserve: 16384,
            keep: 32000,
            progress: 5,
            bg: 300,
            idle: 2,
            kernelMemMb: 4096,
            kernelMaxOpenFiles: 4096,
            kernelMaxProcesses: 256,
            strictSnapshots: true,
            sandboxEnvPassthrough: "",
            botMaxUsdPerDay: null,
            botMaxConsecutiveErrors: 5,
            botWebhookRateLimit: 30,
          },
          configPath: "~/.quartz/config.json",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    });

    render(<SettingsView onBack={vi.fn()} />);

    expect(await screen.findByLabelText("Kernel memory cap (MiB)")).toHaveValue("4096");
    expect(screen.getByLabelText("Sandbox env passthrough")).toHaveValue("MY_APP_ENV");
    expect(screen.getByLabelText("Default daily budget (USD)")).toHaveValue("5");
    expect(await screen.findByText(/Healthy \(v2\)/)).toBeInTheDocument();
    expect(screen.getByText("blocked git -c")).toBeInTheDocument();

    const strictSwitch = screen.getByRole("switch", { name: "Strict snapshot verification" });
    expect(strictSwitch).toHaveAttribute("aria-checked", "true");
    await user.click(strictSwitch);
    expect(posted).toContainEqual({ key: "strict_snapshots", value: "off" });
  });
});
