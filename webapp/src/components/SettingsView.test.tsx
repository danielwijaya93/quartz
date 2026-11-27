import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SettingsView } from "./SettingsView";

describe("SettingsView", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders 4 tabs, navigates to Session & Context, and returns via Back to chat", async () => {
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
    expect(await screen.findByText("Stored in ~/.quartz/config.json")).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Session & Context" }));
    expect(await screen.findByLabelText("Reserve tokens")).toHaveValue("16384");

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
    await user.click(bgSwitch);
    expect(bgSwitch).toHaveAttribute("aria-checked", "false");
    expect(posted).toContainEqual({ key: "notifyBackground", value: "off" });
  });

  it("renders Bots & Budgets with model pricing + unified save bar, and Advanced settings", async () => {
    const user = userEvent.setup();
    const posted: Array<Record<string, unknown>> = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.endsWith("/api/health")) {
        return new Response(JSON.stringify({ status: "ok", version: "0.21.0" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (init?.method === "POST") {
        const body = JSON.parse(String(init.body ?? "{}")) as Record<string, unknown>;
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
              sandboxEnvPassthrough: "MY_APP_ENV",
              botMaxUsdPerDay: 10,
              botMaxConsecutiveErrors: 5,
              botWebhookRateLimit: 30,
              trackChatUsage: true,
              unpricedFallback: { inputPerM: 1, outputPerM: 5 },
              modelPricing: [{ pattern: "google/gemini-4-hydrogen", inputPerM: 0, cachedInputPerM: 0, outputPerM: 0 }],
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
            sandboxEnvPassthrough: "MY_APP_ENV",
            botMaxUsdPerDay: 5,
            botMaxConsecutiveErrors: 5,
            botWebhookRateLimit: 30,
            trackChatUsage: true,
            unpricedFallback: { inputPerM: 1, outputPerM: 5 },
            modelPricing: [],
          },
          defaults: {
            reserve: 16384,
            keep: 32000,
            progress: 5,
            bg: 300,
            idle: 2,
            sandboxEnvPassthrough: "",
            botMaxUsdPerDay: 5,
            botMaxConsecutiveErrors: 5,
            botWebhookRateLimit: 30,
            trackChatUsage: true,
            unpricedFallback: { inputPerM: 1, outputPerM: 5 },
            modelPricing: [],
          },
          recentModels: [{ provider: "google", model: "gemini-4-hydrogen" }],
          configPath: "~/.quartz/config.json",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    });

    render(<SettingsView onBack={vi.fn()} />);

    // Bots & Budgets tab
    await user.click(await screen.findByRole("tab", { name: "Bots & Budgets" }));
    const budgetInput = await screen.findByLabelText("Default daily budget (USD)");
    expect(budgetInput).toHaveValue("5");
    await user.clear(budgetInput);
    await user.type(budgetInput, "10");

    // Quick-add recent model
    await user.click(screen.getByRole("button", { name: "+ google/gemini-4-hydrogen" }));
    expect(screen.getByLabelText("Model pattern 1")).toHaveValue("google/gemini-4-hydrogen");

    // Unified save bar appears and submits batch updates
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(posted.some((p) => typeof p.updates === "object" && p.updates !== null)).toBe(true);

    // Advanced tab
    await user.click(screen.getByRole("tab", { name: "Advanced" }));
    expect(await screen.findByLabelText("Sandbox env passthrough")).toHaveValue("MY_APP_ENV");
  });
});
